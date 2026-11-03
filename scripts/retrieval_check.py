"""Does the runtime's hybrid retrieval (runtime/retrieval.py) reproduce what the experiment found, and does it beat the model alone? Same nine datasets as the cross-encoder
experiment (experiments/cross_encoder/datasets.py), same questions, the pretrained MiniLM. The blend is chosen on the dev questions only; the numbers reported are the TEST
questions (the locked evidence benchmark is not touched here).

    PYTHONPATH=src:. python scripts/retrieval_check.py [--device cpu]

Configurations: embedding · keyword (runtime BM25) · hybrid (runtime, alpha chosen on dev) · hybrid-reference (the experiment's BM25 with alpha 0.3).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from experiments.cross_encoder.datasets import build
from experiments.cross_encoder.rerank import metrics_for, order
from faqengine.bm25 import bm25_entry_scores
from faqengine.encoder import Encoder, pick_device
from faqengine.evaluate import Scorer
from faqengine.heldout_report import ALPHAS, hybrid_scores
from faqengine.runtime.retrieval import BM25, blend, guard_order

ROOT = Path(__file__).resolve().parents[1]


def runtime_keyword(kb, queries) -> np.ndarray:
    index = BM25([v.text for v in kb.views])
    return kb.entry_max(np.stack([index.scores(q) for q in queries]))


def score(mat: np.ndarray, ds, rows: list[int]) -> dict:
    m = [metrics_for(order(mat[i]), ds.gold[i]) for i in rows]
    return {k: float(np.mean([x[k] for x in m])) for k in ("right_first", "mrr@10", "recall@5", "recall@10")} | {"n": len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()
    device = pick_device(args.device)
    base = Encoder.load("minilm-l6").to(device).eval()
    data = build()
    mats = []
    for ds in data:
        scorer = Scorer(base, ds.kb, device)
        mats.append({"dense": scorer.scores(ds.queries), "kw": runtime_keyword(ds.kb, ds.queries), "ref": bm25_entry_scores(ds.kb, ds.queries)})
        print("scored", ds.name, len(ds.queries), flush=True)

    def pooled(alpha: float) -> float:
        vals = []
        for ds, m in zip(data, mats):
            rows = [i for i, p in enumerate(ds.pool) if p == "dev"]
            vals += [metrics_for(order(r), ds.gold[i])["mrr@10"] for r, i in zip(blend_rows(m["dense"][rows], m["kw"][rows], alpha), rows)]
        return float(np.mean(vals)) if vals else 0.0

    def blend_rows(d, k, a):
        return [blend(d[i], k[i], a) for i in range(len(d))]

    mrr = {a: pooled(a) for a in ALPHAS}
    alpha = max(ALPHAS, key=lambda a: (round(mrr[a], 6), -a))
    out = {"alpha_chosen_on_dev": alpha, "dev_mrr_by_alpha": {str(a): round(v, 4) for a, v in mrr.items()}, "datasets": {}}
    agg: dict[str, list] = {}
    for ds, m in zip(data, mats):
        rows = [i for i, p in enumerate(ds.pool) if p == "test"]
        if not rows:
            continue
        cfg = {"embedding": m["dense"], "keyword": m["kw"], "hybrid": hybrid_scores(m["dense"], m["kw"], alpha), "hybrid-reference-0.3": hybrid_scores(m["dense"], m["ref"], 0.3)}
        guarded = np.array([guard_order(order(row), m["dense"][i]) for i, row in enumerate(cfg["hybrid"])])  # the hybrid ranking with the model's best two kept in the first five
        cfg["hybrid+guard"] = -np.argsort(guarded, axis=1).astype(float)  # a score matrix whose order is the guarded ranking
        # the runtime blend is per-question min-max, the same function as hybrid_scores: check that they agree
        assert np.allclose(cfg["hybrid"][rows[0]], blend(m["dense"][rows[0]], m["kw"][rows[0]], alpha), atol=1e-9)
        out["datasets"][ds.name] = {k: score(v, ds, rows) for k, v in cfg.items()}
        for k, v in out["datasets"][ds.name].items():
            agg.setdefault(k, []).append(v)
    summary = {}
    for k, items in agg.items():
        n = sum(x["n"] for x in items)
        summary[k] = {"micro_right_first": sum(x["right_first"] * x["n"] for x in items) / n, "micro_mrr@10": sum(x["mrr@10"] * x["n"] for x in items) / n,
                      "macro_right_first": float(np.mean([x["right_first"] for x in items])), "macro_mrr@10": float(np.mean([x["mrr@10"] for x in items])),
                      "macro_recall@5": float(np.mean([x["recall@5"] for x in items])), "questions": n, "datasets": len(items)}
    out["summary_test"] = summary
    # how many places of the evidence pool to keep for the model's own best matches: chosen on the DEV questions, reported on the test questions
    sweep: dict = {}
    for pool_name in ("dev", "test"):
        sweep[pool_name] = {}
        for g in range(0, 6):
            per_ds = []
            for ds, m in zip(data, mats):
                rows = [i for i, p in enumerate(ds.pool) if p == pool_name]
                if not rows:
                    continue
                comb = hybrid_scores(m["dense"], m["kw"], alpha)
                ms = [metrics_for(guard_order(order(comb[i]), m["dense"][i], g), ds.gold[i]) for i in rows]
                per_ds.append({k: float(np.mean([x[k] for x in ms])) for k in ("right_first", "mrr@10", "recall@5")})
            sweep[pool_name][str(g)] = {k: float(np.mean([d[k] for d in per_ds])) for k in ("right_first", "mrr@10", "recall@5")} | {"datasets": len(per_ds)}
        per_ds = []
        for ds, m in zip(data, mats):
            rows = [i for i, p in enumerate(ds.pool) if p == pool_name]
            if rows:
                ms = [metrics_for(order(m["dense"][i]), ds.gold[i]) for i in rows]
                per_ds.append({k: float(np.mean([x[k] for x in ms])) for k in ("right_first", "mrr@10", "recall@5")})
        sweep[pool_name]["embedding"] = {k: float(np.mean([d[k] for d in per_ds])) for k in ("right_first", "mrr@10", "recall@5")} | {"datasets": len(per_ds)}
    out["guard_sweep"] = sweep
    (ROOT / "runs" / "prod-baseline").mkdir(parents=True, exist_ok=True)
    (ROOT / "runs" / "prod-baseline" / "retrieval.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({"alpha": alpha, **{k: {m: round(x, 3) for m, x in v.items()} for k, v in summary.items()}}, indent=1))


if __name__ == "__main__":
    main()
