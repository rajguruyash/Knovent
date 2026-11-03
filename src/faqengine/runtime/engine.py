"""Standalone FAQ engine for an exported package. It needs only numpy, onnxruntime and tokenizers.

    from engine import Engine
    engine = Engine("path/to/package")
    engine.answer("how long does a transfer take?")
    engine.learn("returns", "can I post it back")      # one more way of asking an entry, searched from now on and kept across restarts

    python engine.py path/to/package "how long does a transfer take?"
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

try:
    from .embedder import Embedder
    from .retrieval import CANDIDATES, DEFAULT_ALPHA, HybridIndex
    from .textutil import normalize_query
    from .validity import inactive_reasons
except ImportError:  # an exported package: loaded by file path, so this folder goes on the path first
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from embedder import Embedder
    from retrieval import CANDIDATES, DEFAULT_ALPHA, HybridIndex
    from textutil import normalize_query
    from validity import inactive_reasons

MAX_QUESTION = 300  # characters in a question the engine will learn
MAX_PER_ENTRY = 60  # learned questions per entry
MAX_LEARNED = 5000  # learned questions in all: a deployment that is being fed junk stops growing instead of slowing down


def _bounds(views: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Where each entry's views start and end (the views of one entry are contiguous)."""
    owner = np.array([v["entry"] for v in views])
    starts = np.flatnonzero(np.r_[True, owner[1:] != owner[:-1]])
    return starts, np.r_[starts[1:], len(owner)]


class Engine:
    def __init__(self, root: str | Path, threads: int = 0, state_dir: str | Path | None = None):
        self.root = Path(root)
        self.state = Path(state_dir) if state_dir else self.root / "data"  # what this deployment has learned since the package was made
        self._lock = threading.Lock()
        self._embedder = Embedder(self.root / "model", threads)
        self.cfg = self._embedder.cfg
        kb = json.loads((self.root / "kb" / "entries.json").read_text())
        self.entries, views = kb["entries"], kb["views"]
        embeddings = self.root / "kb" / "embeddings.npy"
        V = np.load(embeddings).astype(np.float32) if embeddings.exists() else self.embed([v["text"] for v in views], "doc")
        self._ix = (V, views, *_bounds(views))  # replaced as one piece when something is learned, so a question never sees half an update
        self.thresholds = json.loads((self.root / "thresholds.json").read_text())
        self.learned: dict[str, list[str]] = {}
        self._stamp = self._files_stamp()
        self._replay()
        self._learned_stamp = self._learned_file_stamp()

    # the search index: vectors, views, and where each entry's views start and end
    V = property(lambda self: self._ix[0])
    views = property(lambda self: self._ix[1])
    starts = property(lambda self: self._ix[2])
    ends = property(lambda self: self._ix[3])

    def embed(self, texts: list[str], kind: str = "query", batch_size: int = 64) -> np.ndarray:
        return self._embedder.embed(texts, kind, batch_size)

    def scores(self, queries: list[str]) -> np.ndarray:
        """(queries x entries) cosine scores; an entry scores as its best view."""
        V, _, starts, _ = self._ix
        return np.maximum.reduceat(self.embed(queries) @ V.T, starts, axis=1)

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        V, views, starts, ends = self._ix
        sims = V @ self.embed([query])[0]
        best = np.maximum.reduceat(sims, starts)
        hits = []
        for rank, i in enumerate(np.argsort(-best)[:k], start=1):
            e = self.entries[i]
            view = int(starts[i] + np.argmax(sims[starts[i]:ends[i]]))
            hits.append({"rank": rank, "entry_id": e["id"], "question": e["questions"][0], "answer": e["answer"], "kind": e.get("kind", "faq"),
                         "source": e.get("source", ""), "category": e.get("category"), "score": float(best[i]), "matched": views[view]["text"]})
        return hits

    def _entry_dicts(self) -> list[dict]:
        items = [{"id": e["id"], "question": e["questions"][0], "answer": e["answer"], "kind": e.get("kind", "faq"), "source": e.get("source", ""), "category": e.get("category"),
                  **{k: e[k] for k in ("valid_from", "valid_until", "supersedes", "record") if e.get(k)}} for e in self.entries]
        why = inactive_reasons(items)  # what is no longer current today (expired, not yet valid, replaced) is never searched
        return [{**it, "inactive": why[it["id"]]} if it["id"] in why else it for it in items]

    def _index_for_search(self) -> HybridIndex:
        """The hybrid index of the current vectors; rebuilt only when something was learned (the vectors are swapped as one piece, so identity tells)."""
        cached = getattr(self, "_hybrid", None)
        today = time.strftime("%Y-%m-%d")
        if cached is None or cached[0] is not self._ix or cached[2] != today:  # (a new day can retire a source that was valid yesterday)
            V, views, _, _ = self._ix
            cached = self._hybrid = (self._ix, HybridIndex(self._entry_dicts(), views, V), today)
        return cached[1]

    def candidates(self, query: str, n: int = CANDIDATES, method: str | None = None, allowed_sources: set[str] | None = None, diagnostics: dict | None = None, on=None) -> tuple[list[dict], list[dict]]:
        """The best `n` FAQ entries and the best `n` document passages for a question, each best first (the passages are searched on their own, so a FAQ entry can never crowd them
        out). Ranked by the hybrid of the model and keywords (retrieval.py); each candidate carries its similarity, keyword and combined scores. `diagnostics`, if given, is filled in."""
        started = time.perf_counter()
        q = normalize_query(query)
        qv = self.embed([q])[0]
        embed_ms = (time.perf_counter() - started) * 1000
        faq, passages, diag = self._index_for_search().search(q, qv, n, method or self.thresholds.get("retrieval_method", "hybrid"), float(self.thresholds.get("hybrid_alpha", DEFAULT_ALPHA)), allowed_sources, on=on)
        if diagnostics is not None:
            diagnostics.update(diag, embed_ms=embed_ms)
        return faq, passages

    def sentence_similarity(self):
        """sim(part, texts) for the evidence check: cosine between a question part and each text, with the package's own model."""
        def sim(part: str, texts: list[str]) -> list[float]:
            return (self._embedder.embed_docs_cached(texts) @ self.embed([part])[0]).tolist() if texts else []
        return sim

    def respond(self, query: str, llm=None, n: int = CANDIDATES) -> dict:
        """The evidence-aware answer (see answer.py): the stored FAQ answer only when it supports every part of the question, otherwise an answer assembled from the documents with
        references, otherwise an honest "this does not answer it", a report that sources disagree, or a question back. `llm` is optional (runtime/llm.py `LLM`, or a plain
        `llm(prompt) -> str`): it can reword an answer that is already established and suggest other searches, and its text is used only if verify.py passes it."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import answer
        import evidence

        def search(q: str, on=None):
            diag: dict = {}
            faq, passages = self.candidates(q, n, diagnostics=diag, on=on)
            return faq, passages, diag

        search.model = self._index_for_search().fact_model  # the rows of tables, for questions that compare them
        return answer.respond(query, search, self.sentence_similarity(), llm, self.thresholds.get("floor", 0.0), self.thresholds.get("evidence_sim_min", evidence.SEM_MIN))

    def decide(self, score: float) -> str:
        direct = self.thresholds.get("direct")
        if direct is not None and score >= direct:
            return "direct"  # return the stored answer, no LLM needed
        return "llm" if score >= self.thresholds["floor"] else "none"

    def answer(self, query: str, k: int = 3) -> dict:
        """decision is 'direct' (use `answer` as is), 'llm' (pass `context` to your LLM) or 'none' (not in the knowledge base)."""
        started = time.perf_counter()
        hits = self.retrieve(query, k)
        decision = self.decide(hits[0]["score"])
        if decision == "direct" and hits[0]["kind"] != "faq":
            decision = "llm"  # a document passage is context for a person or an LLM, never a curated answer
        return {
            "decision": decision,
            "answer": hits[0]["answer"] if decision == "direct" else None,
            "confidence": hits[0]["score"],
            "context": [{"question": h["question"], "answer": h["answer"], "score": h["score"], "source": h["source"]} for h in hits] if decision == "llm" else [],
            "hits": hits,
            "latency_ms": (time.perf_counter() - started) * 1000,
        }

    # ---- learning: more ways of asking, without retraining
    def learn(self, entry_id: str, question: str, by: str = "approved") -> dict:
        """Search `question` as one more way of asking entry `entry_id`, from now on, and keep it across restarts.

        It is embedded with the model that is already here and added to that entry's views: no retraining, effective at once. The
        questions are also what the next retraining (ci/improve.py) trains on. Raises ValueError for an unknown entry, an empty or long
        question, or when a limit is reached; adding a question the entry already has is not an error and adds nothing."""
        q = re.sub(r"\s+", " ", str(question or "")).strip()
        if not q or len(q) > MAX_QUESTION:
            raise ValueError(f"the question must be 1 to {MAX_QUESTION} characters")
        ids = {e["id"]: i for i, e in enumerate(self.entries)}
        if entry_id not in ids:
            raise ValueError(f"unknown entry_id {entry_id!r}")
        with self._lock:
            if len(self.learned.get(entry_id, ())) >= MAX_PER_ENTRY or sum(map(len, self.learned.values())) >= MAX_LEARNED:
                raise ValueError("this deployment has learned as many questions as it will keep: run ci/improve.py retrain to fold them in")
            if not self._add([(ids[entry_id], q)]):
                return {"added": False, "entry_id": entry_id, "views": len(self.views)}
            self.state.mkdir(parents=True, exist_ok=True)
            with (self.state / "learned.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "entry_id": entry_id, "question": q, "by": by}, ensure_ascii=False) + "\n")
            self._learned_stamp = self._learned_file_stamp()  # our own write is not news
        return {"added": True, "entry_id": entry_id, "views": len(self.views)}

    def _add(self, pairs: list[tuple[int, str]]) -> int:
        """Add (entry index, question) views that are not there yet. Callers hold the lock. Returns how many were new."""
        V, views, starts, ends = self._ix
        have = {(v["entry"], v["text"].lower()) for v in views if v["kind"] == "q"}
        fresh = []
        for i, q in pairs:
            if (i, q.lower()) not in have:
                have.add((i, q.lower()))
                fresh.append((i, q))
        if not fresh:
            return 0
        vectors = self.embed([q for _, q in fresh], "doc")
        by_entry: dict[int, list[int]] = {}
        for k, (i, _) in enumerate(fresh):
            by_entry.setdefault(i, []).append(k)
        new_views, rows = [], []
        for i in range(len(self.entries)):
            new_views += views[starts[i]:ends[i]]
            rows.append(V[starts[i]:ends[i]])
            for k in by_entry.get(i, ()):
                new_views.append({"text": fresh[k][1], "entry": i, "kind": "q"})
                rows.append(vectors[k:k + 1])
                self.learned.setdefault(self.entries[i]["id"], []).append(fresh[k][1])
        self._ix = (np.concatenate(rows).astype(np.float32), new_views, *_bounds(new_views))
        return len(fresh)

    def _replay(self) -> None:
        """Apply what an earlier run learned (data/learned.jsonl), in one batch."""
        path = self.state / "learned.jsonl"
        if not path.is_file():
            return
        ids = {e["id"]: i for i, e in enumerate(self.entries)}
        pairs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row["entry_id"] in ids and 0 < len(row["question"]) <= MAX_QUESTION:
                    pairs.append((ids[row["entry_id"]], row["question"]))
            except (ValueError, KeyError, TypeError):
                continue  # a half-written last line must not stop the engine from starting
        with self._lock:
            self._add(pairs[:MAX_LEARNED])

    # ---- noticing that a new version was swapped in
    def _files_stamp(self) -> tuple:
        stamp = []
        for rel in ("kb/entries.json", "kb/embeddings.npy", "thresholds.json", "model/engine.json"):
            try:
                st = (self.root / rel).stat()
                stamp.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def _learned_file_stamp(self) -> tuple | None:
        try:
            st = (self.state / "learned.jsonl").stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def stale(self) -> bool:
        """True when the package's files were replaced since this engine loaded them (a new version was promoted), or someone else
        (ci/improve.py add) taught the deployment a question."""
        return self._files_stamp() != self._stamp or self._learned_file_stamp() != self._learned_stamp


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python engine.py <package dir> \"question\"")
    print(json.dumps(Engine(sys.argv[1]).answer(sys.argv[2]), indent=2))
