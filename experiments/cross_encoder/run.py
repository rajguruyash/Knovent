"""The experiment. Five configurations, the same questions for each:

    keyword          BM25 over the entries' phrasings (the engine's keyword search)
    embedding        the pretrained MiniLM embedding retrieval (the engine's embedding search)
    hybrid           embedding + keyword, blend chosen on the dev pool
    embedding+ce     the top 20 of `embedding`, reordered by a cross-encoder reading (question, candidate question + answer)
    hybrid+ce        the top 20 of `hybrid`, reordered the same way

    PYTHONPATH=src:. python -m experiments.cross_encoder.run [--device mps]

Phase 1 scores every candidate model on the DEV pool only and picks one by a rule fixed beforehand (highest dev MRR@10 of embedding+ce; within 0.005, the smaller model).
Phase 2 reports the chosen model on the TEST pool and, once, on the LOCKED pool. The other models' test numbers are shown for information; nothing is chosen from them.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch

from experiments.cross_encoder.datasets import Dataset, build
from experiments.cross_encoder.rerank import MODELS, TOP, CrossEncoder, candidate_text, metrics_for, order, rerank
from faqengine.bm25 import bm25_entry_scores
from faqengine.encoder import Encoder, pick_device, release_memory
from faqengine.evaluate import Scorer
from faqengine.heldout_report import ALPHAS, hybrid_scores

OUT = Path(__file__).resolve().parents[2] / "runs" / "cross-encoder"
CONFIGS = ("keyword", "embedding", "hybrid", "embedding+ce", "hybrid+ce")
CONFUSABLE = 0.70  # an entry whose nearest other entry is at least this alike (cosine of the entries' centres) has a hard negative next to it


def prepare(ds: Dataset, base: Encoder, device) -> dict:
    scorer = Scorer(base, ds.kb, device)
    dense, keyword = scorer.scores(ds.queries), bm25_entry_scores(ds.kb, ds.queries)
    centres = np.add.reduceat(scorer.V, ds.kb.view_starts, axis=0)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True).clip(min=1e-9)
    sim = centres @ centres.T
    np.fill_diagonal(sim, -1)
    return {"dense": dense, "keyword": keyword, "nearest": sim.max(1)}


def choose_alpha(data: list[Dataset], prep: list[dict]) -> tuple[float, dict]:
    mrr = {}
    for a in ALPHAS:
        vals = []
        for ds, p in zip(data, prep):
            rows = [i for i, pool in enumerate(ds.pool) if pool == "dev"]
            if rows:
                h = hybrid_scores(p["dense"][rows], p["keyword"][rows], a)
                vals += [metrics_for(order_row, ds.gold[i])["mrr@10"] for order_row, i in zip((order(r) for r in h), rows)]
        mrr[a] = float(np.mean(vals))
    return max(ALPHAS, key=lambda a: (round(mrr[a], 6), -a)), mrr


def evaluate(ds: Dataset, p: dict, alpha: float, ce: CrossEncoder | None, pools: set[str], cache: dict, keep: list | None = None) -> list[dict]:
    """One result row per question in `pools` and configuration."""
    hybrid = hybrid_scores(p["dense"], p["keyword"], alpha)
    base_scores = {"keyword": p["keyword"], "embedding": p["dense"], "hybrid": hybrid}
    texts = [candidate_text(e) for e in ds.kb.entries]
    rows = []
    for i, q in enumerate(ds.queries):
        if ds.pool[i] not in pools:
            continue
        gold = ds.gold[i]
        for cfg in CONFIGS:
            if cfg.endswith("+ce"):
                if ce is None:
                    continue
                s = base_scores[cfg[:-3]][i]
                cand = order(s)[:TOP]
                key = (ds.name, i)
                have = cache.setdefault(key, {})
                need = [int(e) for e in cand if int(e) not in have]
                if need:
                    for e, v in zip(need, ce.score(q, [texts[e] for e in need])):
                        have[e] = float(v)
                out = rerank(s, np.array([have[int(e)] for e in cand]))
                ranking, record = out["final"], out["candidates"]
            else:
                s = base_scores[cfg][i]
                ranking, record = order(s), None
            m = metrics_for(ranking, gold)
            row = {"dataset": ds.name, "q": i, "pool": ds.pool[i], "category": ds.category[i], "config": cfg, **m,
                   "confusable": bool(any(p["nearest"][g] >= CONFUSABLE for g in gold)), "baseline_rank": metrics_for(order(base_scores[cfg[:-3]][i] if cfg.endswith("+ce") else s), gold)["best_gold_rank"]}
            rows.append(row)
            if keep is not None and record is not None:
                keep.append({"dataset": ds.name, "question": q, "config": cfg, "gold": sorted(int(g) for g in gold), "candidates": record})
    return rows


def summarise(rows: list[dict], **where) -> dict:
    sel = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
    if not sel:
        return {}
    keys = ("right_first", "recall@5", "recall@10", "recall@20", "mrr@10", "ndcg@10")
    return {"n": len(sel), **{k: float(np.mean([r[k] for r in sel])) for k in keys}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cpu")
    ap.add_argument("--models", nargs="*", default=list(MODELS))
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    device = pick_device("auto")
    base = Encoder.load("minilm-l6").to(device).eval()
    data = build()
    prep = [prepare(ds, base, device) for ds in data]
    alpha, alpha_mrr = choose_alpha(data, prep)
    print(f"hybrid blend chosen on the dev pool: {alpha} (dev MRR@10 by blend: { {k: round(v, 3) for k, v in alpha_mrr.items()} })", flush=True)
    result = {"alpha": alpha, "alpha_dev_mrr": {str(k): v for k, v in alpha_mrr.items()}, "datasets": {d.name: {"entries": len(d.kb), "questions": len(d.queries)} for d in data},
              "models": {}, "device": args.device}

    caches: dict[str, dict] = {m: {} for m in args.models}
    rows_by_model: dict[str, list[dict]] = {}
    for name in args.models:  # phase 1 (dev) and the informational test numbers
        ce = CrossEncoder(MODELS[name], args.device)
        t0 = time.time()
        rows = []
        for ds, p in zip(data, prep):
            rows += evaluate(ds, p, alpha, ce, {"dev", "test"}, caches[name])
        rows_by_model[name] = rows
        result["models"][name] = {"dev": {c: summarise(rows, config=c, pool="dev") for c in CONFIGS}, "test": {c: summarise(rows, config=c, pool="test") for c in CONFIGS},
                                  "seconds": round(time.time() - t0, 1)}
        print(f"{name:15s} dev MRR@10 embedding {result['models'][name]['dev']['embedding']['mrr@10']:.3f} -> +ce {result['models'][name]['dev']['embedding+ce']['mrr@10']:.3f}"
              f" | hybrid {result['models'][name]['dev']['hybrid']['mrr@10']:.3f} -> +ce {result['models'][name]['dev']['hybrid+ce']['mrr@10']:.3f} ({result['models'][name]['seconds']} s)", flush=True)
        del ce
        release_memory()
    size = {m: sum(p.numel() for p in CrossEncoder(MODELS[m], "cpu").model.parameters()) for m in args.models}
    ranked = sorted(args.models, key=lambda m: (-round(result["models"][m]["dev"]["embedding+ce"]["mrr@10"], 6), size[m]))
    best = ranked[0]
    for m in ranked[1:]:  # within 0.005 of the best on dev: the smaller model
        if result["models"][best]["dev"]["embedding+ce"]["mrr@10"] - result["models"][m]["dev"]["embedding+ce"]["mrr@10"] <= 0.005 and size[m] < size[best]:
            best = m
    result["chosen"] = best
    print("chosen on the dev pool by the preset rule:", best, flush=True)

    ce = CrossEncoder(MODELS[best], args.device)  # phase 2: the chosen model on the locked pool, once, and the per-question records
    keep: list = []
    locked = []
    for ds, p in zip(data, prep):
        if "locked" in ds.pool:
            locked += evaluate(ds, p, alpha, ce, {"locked"}, caches[best], keep)
    for ds, p in zip(data, prep):
        evaluate(ds, p, alpha, ce, {"test"}, caches[best], keep)  # records for the test pool (scores are cached)
    result["locked"] = {c: summarise(locked, config=c) for c in CONFIGS}
    rows = rows_by_model[best] + locked
    result["rows"] = rows
    result["by_category"] = {pool: {cat: {c: summarise(rows, config=c, pool=pool, category=cat) for c in CONFIGS} for cat in sorted({r["category"] for r in rows if r["pool"] == pool})} for pool in ("test", "locked")}
    result["by_dataset"] = {ds.name: {c: summarise(rows, config=c, pool="test", dataset=ds.name) for c in CONFIGS} for ds in data}
    result["confusable"] = {c: summarise(rows, config=c, pool="test", confusable=True) for c in CONFIGS}
    result["not_confusable"] = {c: summarise(rows, config=c, pool="test", confusable=False) for c in CONFIGS}
    (OUT / "results.json").write_text(json.dumps(result, indent=1))
    with (OUT / "records.jsonl").open("w") as f:
        for r in keep:
            f.write(json.dumps(r) + "\n")
    print("wrote", OUT / "results.json", flush=True)


if __name__ == "__main__":
    main()
