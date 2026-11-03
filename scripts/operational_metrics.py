"""Operational numbers for the answering path on this machine: embedding, indexing, search and whole-answer latency, memory. Nothing here uses a language model or the network.

    PYTHONPATH=src python scripts/operational_metrics.py
"""
from __future__ import annotations

import json
import random
import resource
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from faqengine.runtime import answer, evidence  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402
from faqengine.runtime.retrieval import HybridIndex  # noqa: E402

WORDS = "refund delivery warranty booking cancel price opening hours bath nail trim vaccine appointment deposit invoice return exchange shipping address phone email member discount student parking tyre brake repair service".split()


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def main() -> None:
    rng = random.Random(0)
    emb = Embedder(ROOT / "hosted_models" / "minilm-l6" / "model")
    out: dict = {"machine": "this computer (macOS, Apple silicon, CPU only); one thread pool, nothing else busy"}
    q = ["How long does a refund take for my order?", "Do you open on Sundays?", "Can I cancel my booking and get a deposit back?", "What does a basic service cost?"] * 25
    ts = []
    for text in q:
        t = time.perf_counter()
        emb.embed([text], "query")
        ts.append((time.perf_counter() - t) * 1000)
    out["embed_one_question_ms"] = {"p50": round(statistics.median(ts), 2), "p95": round(pct(ts, 95), 2)}
    for n in (1_000, 5_000, 20_000):
        texts = [" ".join(rng.choices(WORDS, k=rng.randint(6, 14))) + f" item{i}" for i in range(n)]
        t = time.perf_counter()
        vecs = emb.embed(texts, "doc")
        embed_s = time.perf_counter() - t
        entries = [{"id": f"e{i}", "question": texts[i], "answer": "Answer " + texts[i], "kind": "faq", "source": "s", "category": None} for i in range(n)]
        views = [{"text": texts[i], "entry": i, "kind": "q"} for i in range(n)]
        t = time.perf_counter()
        index = HybridIndex(entries, views, vecs)
        build_s = time.perf_counter() - t
        lat = []
        for text in q:
            qv = emb.embed([text], "query")[0]
            t = time.perf_counter()
            index.search(text, qv, 8)
            lat.append((time.perf_counter() - t) * 1000)
        out[f"{n}_entries"] = {"embedding_all_views_s": round(embed_s, 2), "index_build_s": round(build_s, 3), "search_ms_p50": round(statistics.median(lat), 2), "search_ms_p95": round(pct(lat, 95), 2),
                               "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20)}
    # a whole answer (retrieval + evidence + response, no model) on the example knowledge base
    from faqengine.kb import load_kb

    kb = load_kb([ROOT / "examples" / "evidence" / "pawfect_faq.csv", ROOT / "examples" / "evidence" / "pawfect_handbook.md", ROOT / "examples" / "evidence" / "policy_update.md"])
    entries = [{"id": e.id, "question": e.question, "answer": e.answer, "kind": e.kind, "source": e.source, "category": e.category} for e in kb.entries]
    views = [{"text": v.text, "entry": v.entry, "kind": v.kind} for v in kb.views]
    vecs = emb.embed([v["text"] for v in views], "doc")
    index = HybridIndex(entries, views, vecs)

    def search(text):
        return (*index.search(text, emb.embed([text], "query")[0], 8),)

    sim = lambda part, texts: (emb.embed_docs_cached(texts) @ emb.embed([part], "query")[0]).tolist() if texts else []  # noqa: E731
    questions = [r.split(",")[3] for r in (ROOT / "examples" / "evidence" / "dev.csv").read_text().splitlines()[1:]] + ["Which pets do you groom?", "What fee applies if I collect my dog late?", "Do you offer dog daycare?"]
    first, lat = [], []
    for round_no in range(4):
        for text in questions:
            t = time.perf_counter()
            answer.respond(text, search, sim, None, 0.0, evidence.SEM_MIN)
            (first if round_no == 0 else lat).append((time.perf_counter() - t) * 1000)
    out["whole_answer_no_model_ms"] = {"questions": len(questions), "knowledge_entries": len(entries), "first_time_p50": round(statistics.median(first), 1), "first_time_p95": round(pct(first, 95), 1),
                                       "warm_p50": round(statistics.median(lat), 1), "warm_p95": round(pct(lat, 95), 1), "note": "warm = the stored sentences' vectors were already computed by earlier questions"}
    print(json.dumps(out, indent=1))
    (ROOT / "runs" / "prod-baseline" / "operational.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
