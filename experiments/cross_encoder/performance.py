"""Measured, not estimated: size, load time, latency (CPU and Apple Silicon GPU), peak memory and end-to-end latency of each cross-encoder on this machine, offline.

    PYTHONPATH=src:. python -m experiments.cross_encoder.performance            # every model, both devices, each in its own process (clean peak memory)
    PYTHONPATH=src:. python -m experiments.cross_encoder.performance --one tinybert-l2 cpu
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20  # bytes on macOS


def one(model: str, device: str) -> dict:
    import torch

    from experiments.cross_encoder.rerank import MODELS, TOP, CrossEncoder, candidate_text, order
    from faqengine.bm25 import bm25_entry_scores
    from faqengine.encoder import Encoder
    from faqengine.evaluate import Scorer
    from faqengine.kb import load_kb

    torch.manual_seed(0)
    kb = load_kb([ROOT / "examples" / "pawfect" / "pawfect_faq.csv", ROOT / "examples" / "pawfect" / "pawfect_handbook.md"])
    questions = [q for q in open(ROOT / "examples" / "pawfect" / "heldout.csv", encoding="utf-8").read().splitlines()[1:61]]
    questions = [q.split(",")[0] for q in questions]
    texts = [candidate_text(e) for e in kb.entries]
    cpu = torch.device("cpu")
    before = rss_mb()
    t0 = time.perf_counter()
    base = Encoder.load("minilm-l6").to(cpu).eval()
    scorer = Scorer(base, kb, cpu)
    embed_load = time.perf_counter() - t0
    after_embedding = rss_mb()
    t0 = time.perf_counter()
    ce = CrossEncoder(MODELS[model], device)
    load = time.perf_counter() - t0
    after_load = rss_mb()
    params = sum(p.numel() for p in ce.model.parameters())
    for q in questions[:5]:  # warm up (the first call on a device compiles kernels)
        ce.score(q, texts[:TOP])
    pairs, per_query = [], []
    for q in questions:
        t = time.perf_counter()
        ce.score(q, (texts * 2)[:TOP])  # a query's 20 candidates in one batch
        if device == "mps":
            torch.mps.synchronize()
        per_query.append((time.perf_counter() - t) * 1000)
    single = []
    for q in questions[:20]:  # one pair at a time: the worst case for batching
        t = time.perf_counter()
        ce.score(q, texts[:1], batch=1)
        single.append((time.perf_counter() - t) * 1000)
    # end to end, one question at a time, the way the engine answers: embed the question, score every entry, keep the top 20, (rerank them)
    e2e_plain, e2e_ce = [], []
    for q in questions:
        t = time.perf_counter()
        dense = scorer.scores([q])[0]
        keyword = bm25_entry_scores(kb, [q])[0]
        cand = order(dense)[:TOP]
        plain = (time.perf_counter() - t) * 1000
        t2 = time.perf_counter()
        ce.score(q, [texts[int(e)] for e in cand])
        if device == "mps":
            torch.mps.synchronize()
        e2e_plain.append(plain)
        e2e_ce.append(plain + (time.perf_counter() - t2) * 1000)
    ps = lambda a: {"median": round(float(np.median(a)), 2), "p95": round(float(np.percentile(a, 95)), 2)}  # noqa: E731
    return {"model": model, "device": device, "params_millions": round(params / 1e6, 1), "fp32_mb": round(params * 4 / 2**20, 1), "load_seconds": round(load, 2),
            "peak_rss_mb_after_embedding_model": round(after_embedding, 0), "peak_rss_mb_after_load": round(after_load, 0), "peak_rss_mb_after_inference": round(rss_mb(), 0),
            "extra_peak_rss_mb_for_the_reranker": round(rss_mb() - after_embedding, 0), "latency_ms_20_candidates": ps(per_query), "latency_ms_one_pair": ps(single),
            "end_to_end_ms_without_reranker_cpu": ps(e2e_plain), "end_to_end_ms_with_reranker": ps(e2e_ce), "embedding_model_load_seconds": round(embed_load, 2), "questions": len(questions)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--one", nargs=2, metavar=("MODEL", "DEVICE"))
    ap.add_argument("--out", default=str(ROOT / "runs" / "cross-encoder" / "performance.json"))
    args = ap.parse_args()
    if args.one:
        print(json.dumps(one(*args.one)))
        return
    from experiments.cross_encoder.rerank import MODELS

    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"}  # offline: nothing may be downloaded
    out = []
    for model in MODELS:
        for device in ("cpu", "mps"):
            r = subprocess.run([sys.executable, "-m", "experiments.cross_encoder.performance", "--one", model, device], capture_output=True, text=True, env=env, cwd=ROOT, timeout=1200)
            line = next((x for x in r.stdout.splitlines() if x.startswith("{")), None)
            out.append(json.loads(line) if line else {"model": model, "device": device, "error": r.stderr[-300:]})
            print(out[-1], flush=True)
    Path(args.out).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
