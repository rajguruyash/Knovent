"""THE RETRIEVAL ENGINE. Finds the entries that might contain the answer to a question, and says how it found them. It does not judge whether they answer it (that is the evidence
engine, evidence.py) and it does not write anything (that is the response engine, response.py).

    query -> normalise -> embed (the caller does this: it owns the model) -> semantic score + keyword score per entry -> hybrid blend -> rank -> drop duplicates -> candidates

Every entry is searched through several views (its question, its alternative phrasings, question plus answer) and scores as its best view, in both channels:
    similarity_score   cosine of the question and the best view (semantic channel). NOT a probability and NOT a confidence that the entry answers: it only says two texts are alike
    keyword_score      BM25 of the question over the views' words (keyword channel); 0 when no word is shared
    combined_score     (1 - alpha) * unit(similarity) + alpha * unit(keyword), each stretched to 0..1 over this question's entries; the ranking uses it, nothing else does
The default alpha is 0.3: chosen on the development questions of the cross-encoder experiment (docs/cross-encoder-experiment.md), where this hybrid was the best of five
configurations. A cross-encoder reranker was tested against it and was worse overall, so none is used.

Standard library and numpy only, so that it runs inside an exported package.
"""
from __future__ import annotations

import math
import time
from collections import Counter, defaultdict

import numpy as np

import re

try:
    from .textutil import STOP, normalize_query, stem, terms
    from .validity import inactive_reasons
except ImportError:  # an exported package puts runtime/ on the path
    from textutil import STOP, normalize_query, stem, terms
    from validity import inactive_reasons

DEFAULT_ALPHA = 0.3  # the keyword share of the blend
CANDIDATES = 10  # how many candidates one search returns, of every kind together: a FAQ entry, a document passage, a spreadsheet row and a web page compete in one ranking
EVIDENCE_POOL = 8  # the first of them that the evidence engine is sure to examine; the model's own best matches are kept inside it
DEFAULT_GUARD = 4  # of those, the places kept for the model's own best matches (see guard_order): chosen on the development questions of nine datasets (docs/retrieval-architecture.md)
METHODS = {"hybrid": None, "semantic": 0.0, "keyword": 1.0}  # keyword-only exists for diagnostics and comparison, not for answering


class BM25:
    """Okapi BM25 over a list of texts (k1 1.5, b 0.75: the usual values, the same as the keyword baseline in bm25.py)."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b, self.n = k1, b, len(docs)
        toks = [terms(d) for d in docs]
        self.lengths = np.array([len(t) for t in toks], dtype=float)
        self.avg = max(float(self.lengths.mean()) if len(docs) else 0.0, 1e-9)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, t in enumerate(toks):
            for term, tf in Counter(t).items():
                self.postings[term].append((i, tf))
        self.idf = {t: math.log((self.n - len(p) + 0.5) / (len(p) + 0.5) + 1.0) for t, p in self.postings.items()}

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(self.n)
        for term in set(terms(query)):
            for i, tf in self.postings.get(term, ()):
                norm = 1 - self.b + self.b * self.lengths[i] / self.avg
                out[i] += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        return out


def unit(m: np.ndarray) -> np.ndarray:
    """One question's scores over the entries stretched to 0..1 so that a cosine and a BM25 score can be added (all equal -> all 0)."""
    lo, hi = float(m.min()), float(m.max())
    return (m - lo) / max(hi - lo, 1e-9)


def guard_order(order: np.ndarray, similarity: np.ndarray, guard: int = DEFAULT_GUARD, pool: int = EVIDENCE_POOL) -> np.ndarray:
    """Keeps the model's `guard` most similar candidates inside the first `pool` places of a ranking made by the blend. Blending with keywords raises the right entry more often than it
    lowers it (right-first and MRR go up), but it can push a candidate the model had in its top few just out of the few the evidence engine reads; this guarantees that cannot happen
    to the best few. The order inside the pool stays the blend's; only membership changes."""
    if guard <= 0 or len(order) <= pool:
        return order
    sem = set(order[np.lexsort((order, -similarity[order]))][:guard].tolist())
    head = order[:pool].tolist()
    if sem <= set(head):
        return order
    keep = set(sem) | set([i for i in head if i not in sem][:max(pool - len(sem), 0)])  # the protected ones, wherever they were, and the best of the rest
    new_head = [i for i in order.tolist() if i in keep]  # the blend's order, restricted to who stays
    return np.array(new_head + [i for i in order.tolist() if i not in keep], dtype=order.dtype)


def blend(similarity: np.ndarray, keyword: np.ndarray, alpha: float) -> np.ndarray:
    return (1 - alpha) * unit(similarity) + alpha * unit(keyword)


class HybridIndex:
    """The searchable form of a knowledge base: entries (dicts with id, question, answer, kind, source, category), their views (dicts with text, entry, kind) and one vector per view."""

    def __init__(self, entries: list[dict], views: list[dict], vectors: np.ndarray):
        if len(views) != len(vectors):
            raise ValueError("there must be one vector per view")
        self.entries, self.views, self.V = entries, views, vectors
        owner = np.array([v["entry"] for v in views])
        self.starts = np.flatnonzero(np.r_[True, owner[1:] != owner[:-1]]) if len(owner) else np.zeros(0, int)
        self.ends = np.r_[self.starts[1:], len(owner)] if len(owner) else np.zeros(0, int)
        self.bm25 = BM25([v["text"] for v in views])
        self.vocab = {t for v in views for t in terms(v["text"])}  # every word the knowledge uses, to tell a name that it never mentions

    def reasons_on(self, day) -> dict[str, str]:
        """Which entries are not current on `day` (the day a question is about, which is not always today): by what their sources say about themselves. Cached per day."""
        cache = self.__dict__.setdefault("_reasons", {})
        if day not in cache:
            cache[day] = inactive_reasons([{"id": e["id"], "source": e.get("source", ""), "valid_from": e.get("valid_from"), "valid_until": e.get("valid_until"), "supersedes": e.get("supersedes")}
                                           for e in self.entries], day)
        return cache[day]

    def records(self) -> list[dict]:
        """The rows of tables of values that are current, as the data they were read from (entries with a `record`): what structured reasoning compares. A table's rows are all here, not
        only the ones a question happened to retrieve, because "which plan is cheapest" is about every plan."""
        return [{"entry_id": e["id"], "question": e["question"], "answer": e.get("answer", ""), "kind": e.get("kind", "passage"), "source": e.get("source", ""), "record": e["record"]}
                for e in self.entries if e.get("record") and not e.get("inactive")]

    def fact_model(self):
        """The rows read as facts (see structured.py), built once."""
        if getattr(self, "_model", None) is None:
            try:
                from . import structured
            except ImportError:
                import structured
            recs = self.records()
            self._model = structured.Model(recs) if recs else False
        return self._model or None

    def unknown_names(self, query: str) -> list[str]:
        """Names in a question (capitalised words after the first, and acronyms) that appear nowhere in the whole knowledge: "Hindi", "Okta", "Singapore". Nothing retrieved can be
        about something the knowledge never mentions, however close in meaning a sentence is, so the evidence engine does not let similarity stand in for it."""
        out = []
        for k, tok in enumerate(re.findall(r"[^\W_]+", query)):
            if k == 0 or len(tok) < 2 or not tok[0].isupper() or tok.lower() in STOP:
                continue
            st = stem(tok)
            if st not in self.vocab and not (len(st) >= 4 and any(len(v) >= 4 and (v.startswith(st) or st.startswith(v)) for v in self.vocab)):
                out.append(st)
        return out

    def search(self, query: str, qv: np.ndarray, n: int = CANDIDATES, method: str = "hybrid", alpha: float = DEFAULT_ALPHA, allowed_sources: set[str] | None = None,
               guard: int = DEFAULT_GUARD, on=None) -> tuple[list[dict], list[dict], dict]:
        """The best `n` candidates of every kind together, ranked by the blend of meaning and keywords and nothing else: a FAQ entry gets no place of its own and no head start. Returned
        as (FAQ entries, passages, diagnostics), each in rank order, every candidate carrying its overall `rank`. Entries that are not current (expired, not yet valid, replaced by a newer
        source) are never candidates; the best few of them are named in the diagnostics (`inactive_matches`) so that a question only they answer can be told so.
        `qv` is the question's vector from the same model as the index. `allowed_sources` limits the search to entries from those sources."""
        started = time.perf_counter()
        if method not in METHODS:
            raise ValueError(f"unknown retrieval method {method!r}")
        a = alpha if METHODS[method] is None else METHODS[method]
        q = normalize_query(query)
        view_sim = self.V @ qv
        view_kw = self.bm25.scores(q)
        sim = np.maximum.reduceat(view_sim, self.starts)
        kw = np.maximum.reduceat(view_kw, self.starts)
        combined = blend(sim, kw, a)
        order = np.lexsort((np.arange(len(combined)), -combined))  # best first; a tie keeps the entry that comes first, so the order is reproducible
        reasons = self.reasons_on(on) if on is not None else None  # the day the question is about, when it is not today
        active, inactive = [], []
        for i in order:
            e = self.entries[int(i)]
            if allowed_sources is not None and e.get("source", "") not in allowed_sources:
                continue
            if (reasons.get(e["id"]) if reasons is not None else e.get("inactive")):
                if len(inactive) < 3:
                    inactive.append(int(i))
                continue
            active.append(int(i))
        ranked = guard_order(np.array(active, dtype=int), sim, guard, EVIDENCE_POOL).tolist() if (method == "hybrid" and guard and active) else active  # the model's best few stay in the pool
        chosen, dropped, seen = [], 0, {}

        def hit_of(i: int, rank: int) -> dict:
            e = self.entries[i]
            kind = e.get("kind", "faq")
            lo, hi = int(self.starts[i]), int(self.ends[i])
            vi = lo + int(np.argmax(view_sim[lo:hi]))
            return {"rank": rank, "entry_id": e["id"], "question": e["question"], "answer": e.get("answer", ""), "kind": kind, "source": e.get("source", ""),
                    "category": e.get("category"), "score": float(sim[i]), "similarity_score": float(sim[i]), "keyword_score": float(kw[i]), "combined_score": float(combined[i]),
                    "retrieval_method": "hybrid (semantic only: no word in common)" if method == "hybrid" and kw[i] <= 0 else method,
                    "matched": self.views[vi]["text"], "matched_kind": self.views[vi]["kind"],
                    "location": {"source": e.get("source", ""), "section": e["question"] if kind != "faq" else None, "entry": e["id"]}, "duplicates": [],
                    **{k: e[k] for k in ("valid_from", "valid_until", "record") if e.get(k)}}

        for i in ranked:
            if len(chosen) >= n:
                break
            e = self.entries[i]
            key = (e.get("kind", "faq"), " ".join(str(e.get("answer", "")).lower().split()))
            if key[1] and key in seen:  # the same text under another id (a page crawled twice, an entry pasted twice): the better-ranked one stands, the other is named
                seen[key]["duplicates"].append(e["id"])
                dropped += 1
                continue
            hit = hit_of(i, len(chosen) + 1)
            if key[1]:
                seen[key] = hit
            chosen.append(hit)
        old = [{**hit_of(i, k + 1), "inactive": (reasons.get(self.entries[i]["id"]) if reasons is not None else self.entries[i]["inactive"])} for k, i in enumerate(inactive)]
        diag = {"method": method, "alpha": a, "entries": len(self.entries), "views": len(self.views), "entries_with_keyword_match": int((kw > 0).sum()), "duplicates_dropped": dropped,
                "source_filter": sorted(allowed_sources) if allowed_sources is not None else None, "search_ms": (time.perf_counter() - started) * 1000,
                "candidates": len(chosen), "kinds": {k: sum(h["kind"] == k for h in chosen) for k in ("faq", "passage")}, "inactive_matches": old, "unknown_names": self.unknown_names(query),
                "note": "similarity_score is a cosine between texts, not a probability; combined_score only orders candidates"}
        return [h for h in chosen if h["kind"] == "faq"], [h for h in chosen if h["kind"] != "faq"], diag
