"""Answers one question at a time for the website, from a finished run, on the CPU (training keeps the GPU's memory to itself)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

from ..kb import KB, load_kb, write_kb
from ..runtime.retrieval import CANDIDATES, DEFAULT_ALPHA, HybridIndex
from ..runtime.validity import inactive_reasons
from ..runtime.textutil import normalize_query
from .store import read_json


class ProjectEngine:
    def __init__(self, run_dir: str | Path):
        import torch

        from ..encoder import Encoder

        self.run = Path(run_dir)
        self.base_kb = load_kb(self.run / "kb.json")
        self.cpu = torch.device("cpu")
        self.enc = Encoder.load(self.run / "model").to(self.cpu).eval()
        self.base_V = np.load(self.run / "views.npy")
        if len(self.base_V) != len(self.base_kb.views):
            raise RuntimeError("the saved vectors do not match the FAQ: train again")
        self._index = (self.base_kb, self.base_V)  # swapped as one piece, so a question never sees new views with old vectors
        self.learned: dict[str, set[str]] = {}
        self.map = (read_json(self.run / "report.json") or {}).get("map")
        self.stamp = (self.run / "report.json").stat().st_mtime

    @property
    def kb(self) -> KB:
        return self._index[0]

    def current(self) -> bool:
        try:
            return (self.run / "report.json").stat().st_mtime == self.stamp
        except OSError:
            return False

    def learn(self, extras: dict[str, list[str]]) -> int:
        """Search questions a person approved or corrected as extra views of their entries from now on, with no retraining.

        `extras` is every such question (entry id -> questions); calling it again with more simply adds the new ones. Only the new
        views are embedded. Returns how many views are new since the last call."""
        from ..auto import with_extras

        base = self.base_kb
        kb = KB(with_extras(base.entries, extras), base.warnings)
        have = {(base.entries[v.entry].id, v.kind, v.text): i for i, v in enumerate(base.views)}
        V = np.zeros((len(kb.views), self.base_V.shape[1]), np.float32)
        fresh = []
        for i, v in enumerate(kb.views):
            j = have.get((kb.entries[v.entry].id, v.kind, v.text))
            if j is None:
                fresh.append(i)
            else:
                V[i] = self.base_V[j]
        if fresh:
            V[fresh] = self.enc.encode([kb.views[i].text for i in fresh], "doc", device=self.cpu)
        before = len(self._index[0].views)
        self._index = (kb, V)
        original = {e.id: {q.lower() for q in e.questions} for e in base.entries}
        self.learned = {eid: {q.lower() for q in qs} - original.get(eid, set()) for eid, qs in extras.items()}
        return len(kb.views) - before

    def reindex(self, kb: KB) -> dict:
        """Search a changed set of files with the model as it is, no training: the vectors of views whose text did not change are kept, only new or changed views are embedded. Everything
        is computed before anything is replaced, in memory and on disk, so a failure leaves the index as it was. Returns what was kept, embedded and dropped."""
        old = {(v.kind, v.text): i for i, v in enumerate(self.base_kb.views)}
        V = np.zeros((len(kb.views), self.base_V.shape[1]), np.float32)
        fresh = []
        for i, v in enumerate(kb.views):
            j = old.get((v.kind, v.text))
            if j is None:
                fresh.append(i)
            else:
                V[i] = self.base_V[j]
        if fresh:
            V[fresh] = self.enc.encode([kb.views[i].text for i in fresh], "doc", device=self.cpu)
        kept = len(kb.views) - len(fresh)
        tmp_kb, tmp_v = self.run / "kb.new.json", self.run / "views.new.npy"  # (write_kb picks the format from the suffix: it must end in .json)
        write_kb(kb.entries, tmp_kb)
        np.save(tmp_v, V)
        os.replace(tmp_v, self.run / "views.npy")  # (the two files are replaced one after the other, the vectors first: a reader sees either the old pair or, at worst, vectors for the new text)
        os.replace(tmp_kb, self.run / "kb.json")
        self.base_kb, self.base_V = kb, V
        self._index = (kb, V)
        self.learned = {}
        return {"views": len(kb.views), "kept": kept, "embedded": len(fresh), "dropped": len(old) - len({(v.kind, v.text) for v in kb.views} & set(old)), "entries": len(kb.entries)}

    def _index_for_search(self) -> HybridIndex:
        """The hybrid index of the current knowledge base and vectors (rebuilt only when something was learned: the pair is swapped as one piece, so identity tells)."""
        cached = getattr(self, "_hybrid", None)
        today = time.strftime("%Y-%m-%d")
        if cached is None or cached[0] is not self._index or cached[2] != today:
            kb, V = self._index
            entries = [{"id": e.id, "question": e.question, "answer": e.answer, "kind": e.kind, "source": e.source, "category": e.category,
                        **{k: getattr(e, k) for k in ("valid_from", "valid_until", "supersedes", "record") if getattr(e, k)}} for e in kb.entries]
            why = inactive_reasons(entries)  # expired, not yet valid or replaced: never searched, never used to answer
            entries = [{**e, "inactive": why[e["id"]]} if e["id"] in why else e for e in entries]
            views = [{"text": v.text, "entry": v.entry, "kind": v.kind} for v in kb.views]
            cached = self._hybrid = (self._index, HybridIndex(entries, views, V), today)
        return cached[1]

    def candidates(self, query: str, n: int = CANDIDATES, method: str = "hybrid", alpha: float = DEFAULT_ALPHA, allowed_sources: set[str] | None = None,
                   diagnostics: dict | None = None, on=None) -> tuple[list[dict], list[dict]]:
        """The best `n` FAQ entries and the best `n` document passages for a question (each kind searched on its own), best first, ranked by the hybrid of the model and keywords
        (runtime/retrieval.py). Each candidate carries its similarity, keyword and combined scores; `diagnostics`, if given, is filled in."""
        started = time.perf_counter()
        q = normalize_query(query)
        qv = self.enc.encode([q], "query", device=self.cpu)[0]
        embed_ms = (time.perf_counter() - started) * 1000
        faq, passages, diag = self._index_for_search().search(q, qv, n, method, alpha, allowed_sources, on=on)
        if diagnostics is not None:
            diagnostics.update(diag, embed_ms=embed_ms)
        return faq, passages

    def sentence_similarity(self):
        """sim(part, texts): cosine between a question part and each text by the project's model (what the evidence check uses for different wording)."""
        def sim(part: str, texts: list[str]) -> list[float]:
            return (self.enc.encode(texts, "doc", device=self.cpu) @ self.enc.encode([part], "query", device=self.cpu)[0]).tolist() if texts else []
        return sim

    def respond(self, query: str, thresholds: dict, llm=None, n: int = CANDIDATES, module=None, pipeline: str = "off") -> dict:
        """The evidence-aware answer for a question (runtime/answer.py): what the website shows next to the closest entries. `module` swaps the evidence module (benchmarks).

        `pipeline` is the same setting as the hosted service's (runtime/shadow.py MODES), and it runs the same code (runtime/pipeline.py): "off" the existing path answers; "shadow" it still answers
        and the pipeline is run on the same question and compared, the differences coming back as short phrases (`execution.shadow`), never as a second answer; "on" the pipeline answers, and a
        fault in it falls back to the existing path. The pipeline here is deterministic and nothing else: it is given no provider and no consent, so it can send nothing anywhere. A project that
        turned its own language model on keeps the existing path, which says so."""
        from ..runtime import answer, evidence, shadow
        from ..runtime.pipeline import Pipeline

        method, alpha = thresholds.get("retrieval_method", "hybrid"), float(thresholds.get("hybrid_alpha", DEFAULT_ALPHA))
        floor, sem_min = thresholds.get("floor", 0.0), thresholds.get("evidence_sim_min", (module or evidence).SEM_MIN)
        if module is not None:  # an older evidence implementation, for before/after measurements: no model, no diagnostics
            faq, passages = self.candidates(query, n, method, alpha)
            return module.respond(query, faq, passages, llm=llm, similarity_floor=floor, sim=self.sentence_similarity(), sem_min=sem_min)

        def search(q: str, on=None):
            diag: dict = {}
            faq, passages = self.candidates(q, n, method, alpha, diagnostics=diag, on=on)
            return faq, passages, diag

        search.model = self._index_for_search().fact_model  # the rows of tables, for questions that compare them
        sim = self.sentence_similarity()
        mode = shadow.mode_from(pipeline)
        new, note = None, None
        if mode != "off" and llm is None:
            try:
                new = Pipeline("deterministic").run(query, search, sim, similarity_floor=floor, sem_min=sem_min)
                new["execution"] = {"path": "pipeline", **new["execution"]}
            except Exception as e:  # noqa: BLE001 - a fault in the new path must not become a failed answer
                note = f"pipeline_error:{type(e).__name__}"
        elif mode != "off":
            note = "this project's own language model is on"
        if mode == "on" and new is not None:
            return new
        result = answer.respond(query, search, sim, llm, floor, sem_min)
        result["execution"] = shadow.legacy_execution(result, reason=note)
        if mode == "shadow" and new is not None:
            result["execution"]["shadow"] = {"compared": True, "differences": shadow.compare(result, new)}
        return result

    def ask(self, query: str, thresholds: dict, k: int = 3) -> dict:
        started = time.perf_counter()
        kb, V = self._index
        qv = self.enc.encode([query], "query", device=self.cpu)[0]
        sims = V @ qv
        best = np.maximum.reduceat(sims, kb.view_starts)
        hits = []
        for i in np.argsort(-best)[:k]:
            e = kb.entries[int(i)]
            views = kb.views_of(int(i))
            lo = int(kb.view_starts[i])
            v = views[int(np.argmax(sims[lo:lo + len(views)]))]
            hits.append({"id": e.id, "question": e.question, "answer": e.answer, "category": e.category, "kind": e.kind, "source": e.source,
                         "score": float(best[i]), "matched": v.text, "matched_kind": v.kind,
                         "learned": v.kind == "q" and v.text.lower() in self.learned.get(e.id, ())})  # matched a question a person approved earlier
        top = hits[0]["score"]
        direct = thresholds.get("direct")
        # only a curated FAQ answer may be given directly; a document passage is context
        band = "direct" if direct is not None and top >= direct and hits[0]["kind"] == "faq" else "llm" if top >= thresholds["floor"] else "none"
        point = None
        if self.map:
            xy = ((qv - np.array(self.map["mean"])) @ np.array(self.map["components"]).T) / self.map["scale"]
            point = [float(xy[0]), float(xy[1])]
        return {"band": band, "confidence": top, "hits": hits, "point": point, "latency_ms": (time.perf_counter() - started) * 1000}
