"""Ranking metrics for FAQ retrieval, where every question has exactly one right entry.

With one relevant item, Recall@K equals Hit@K and Precision@K is Hit@K / K, so Precision@K can never exceed
1/K. Ties are counted against the system (a query that matches nothing ranks the right entry last), which
keeps BM25 honest when most of its scores are zero.
"""
from __future__ import annotations

import math

import numpy as np

KS = (1, 3, 5)

# two-sided 95% critical values of Student's t by degrees of freedom
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
        12: 2.18, 15: 2.13, 20: 2.09, 30: 2.04}


def ranks(scores: np.ndarray, gold: np.ndarray) -> np.ndarray:
    """1-based rank of the gold entry per query (pessimistic on ties). Out-of-scope rows (gold < 0) are ignored."""
    rows = np.flatnonzero(gold >= 0)
    s = scores[rows]
    g = s[np.arange(len(rows)), gold[rows]][:, None]
    return (s > g).sum(1) + (s == g).sum(1)  # greater, plus ties including itself


def per_query(scores: np.ndarray, gold: np.ndarray) -> dict[str, np.ndarray]:
    r = ranks(scores, gold)
    out = {f"hit@{k}": (r <= k).astype(float) for k in KS}
    out.update({f"p@{k}": (r <= k) / k for k in KS})
    out["mrr@10"] = np.where(r <= 10, 1.0 / r, 0.0)
    out["ndcg@10"] = np.where(r <= 10, 1.0 / np.log2(r + 1.0), 0.0)
    return out


def summarize(scores: np.ndarray, gold: np.ndarray) -> dict[str, float]:
    return {k: float(v.mean()) for k, v in per_query(scores, gold).items()}


def listwise_loss(scores: np.ndarray, gold: np.ndarray, scale: float) -> float:
    """Cross-entropy of softmax(scale * score) over entries, the quantity training minimises."""
    rows = np.flatnonzero(gold >= 0)
    z = scale * scores[rows]
    z = z - z.max(1, keepdims=True)
    logp = z - np.log(np.exp(z).sum(1, keepdims=True))
    return float(-logp[np.arange(len(rows)), gold[rows]].mean())


def paired_bootstrap(a: np.ndarray, b: np.ndarray, n: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """Mean of (b - a) over queries with a 95% bootstrap interval."""
    d = np.asarray(b, float) - np.asarray(a, float)
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, len(d), size=(n, len(d)))].mean(1)
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def mean_ci(values: list[float]) -> tuple[float, float]:
    """Mean and 95% half-width across independent runs (t interval)."""
    v = np.asarray(values, float)
    if len(v) < 2:
        return float(v.mean()) if len(v) else math.nan, math.nan
    df = len(v) - 1
    t = _T95.get(df) or _T95[min((k for k in _T95 if k >= df), default=30)]
    return float(v.mean()), float(t * v.std(ddof=1) / math.sqrt(len(v)))


def auroc(positive: np.ndarray, negative: np.ndarray) -> float:
    """Probability that a random positive scores above a random negative (ties count half)."""
    if len(positive) == 0 or len(negative) == 0:
        return math.nan
    allv = np.concatenate([positive, negative])
    order = allv.argsort(kind="mergesort")
    rank = np.empty(len(allv))
    rank[order] = np.arange(1, len(allv) + 1)
    # average ranks over ties
    _, inv, counts = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=rank)
    rank = (sums / counts)[inv]
    return float((rank[: len(positive)].sum() - len(positive) * (len(positive) + 1) / 2) / (len(positive) * len(negative)))


def per_query_sets(scores: np.ndarray, gold: list) -> dict[str, np.ndarray]:
    """Metrics when a question may have several acceptable entries (ambiguous questions); with one entry they match `per_query`.

    hit@k: any acceptable entry in the top k. p@k: acceptable entries in the top k, divided by k. r@k: the same, divided by how many are
    acceptable. mrr@10 uses the first acceptable entry. nDCG@10 gives every acceptable entry gain 1 and is divided by the best possible
    ordering. Ties are broken against the system, as everywhere else."""
    n, e = scores.shape
    out = {f"{m}@{k}": np.zeros(n) for m in ("hit", "p", "r") for k in KS}
    out["mrr@10"], out["ndcg@10"] = np.zeros(n), np.zeros(n)
    for i in range(n):
        rel = np.zeros(e, dtype=bool)
        rel[list(gold[i])] = True
        m = int(rel.sum())
        order = np.lexsort((rel, -scores[i]))  # best score first; on a tie the unacceptable entry goes first
        pos = np.flatnonzero(rel[order]) + 1
        for k in KS:
            found = int((pos <= k).sum())
            out[f"hit@{k}"][i], out[f"p@{k}"][i], out[f"r@{k}"][i] = found > 0, found / k, found / m
        first = int(pos.min())
        out["mrr@10"][i] = 1.0 / first if first <= 10 else 0.0
        dcg = sum(1.0 / math.log2(p + 1) for p in pos if p <= 10)
        out["ndcg@10"][i] = dcg / sum(1.0 / math.log2(j + 1) for j in range(1, min(m, 10) + 1))
    return out
