"""How well a business's knowledge is FOUND, measured on itself, with nothing sent anywhere and no model weight changed.

For every FAQ entry that has more than one wording (its question, its alternative questions, and the questions people approved while testing), each wording is held out in turn: the search runs
with that one phrase removed, and we see where the entry lands from the others. That is the situation a customer is in when they word a question their own way. Two searches are measured:
the keyword search (BM25) and MiniLM as downloaded (the same vectors the site answers with). Hits at 1 and 3, MRR@10 and nDCG@10 are computed per held-out wording and averaged.

What this does not do: it does not fine-tune MiniLM (that needs PyTorch and minutes of compute: the local app does it), and an entry with a single wording cannot be checked, so it says how many were.
These are sanity checks on this knowledge, not a promise of accuracy on real customers."""
from __future__ import annotations

import math

import numpy as np

MAX_CHECKS = 1500  # held-out wordings measured per run (evenly spread when there are more): a request must stay fast


def _rank_of(entry_scores: np.ndarray, gold: int) -> int:
    """1-based place of `gold`; entries tied with it do not count as ahead (the same convention a sort would give them on average, and never flattering)."""
    ahead = int((entry_scores > entry_scores[gold]).sum())
    tied = int((entry_scores == entry_scores[gold]).sum()) - 1
    return ahead + 1 + tied // 2 + (tied % 2)


def _summary(ranks: list[int]) -> dict:
    n = max(len(ranks), 1)
    return {"hit@1": sum(r <= 1 for r in ranks) / n, "hit@3": sum(r <= 3 for r in ranks) / n,
            "mrr@10": sum(1 / r for r in ranks if r <= 10) / n, "ndcg@10": sum(1 / math.log2(r + 1) for r in ranks if r <= 10) / n}


def measure(index, model: dict | None = None) -> dict:
    """`index` is a runtime.retrieval.HybridIndex (views grouped by entry, one vector per view)."""
    views, V, starts, ends = index.views, np.asarray(index.V, dtype=np.float32), index.starts, index.ends
    n_entries = len(index.entries)
    q_by_entry: dict[int, list[int]] = {}
    for k, v in enumerate(views):
        if v["kind"] == "q" and index.entries[v["entry"]].get("kind") == "faq":
            q_by_entry.setdefault(v["entry"], []).append(k)
    held = [k for ks in q_by_entry.values() if len(ks) > 1 for k in ks]
    wordings = {e: len(ks) for e, ks in q_by_entry.items()}
    single = sum(1 for c in wordings.values() if c == 1)
    if len(held) > MAX_CHECKS:
        step = len(held) / MAX_CHECKS
        held = [held[int(i * step)] for i in range(MAX_CHECKS)]
    ranks_dense, ranks_kw = [], []
    for k in held:
        gold = views[k]["entry"]
        dense = V @ V[k]
        dense[k] = -np.inf  # the held-out wording itself is not allowed to find its own entry
        ranks_dense.append(_rank_of(np.maximum.reduceat(dense, starts), gold))
        kw = index.bm25.scores(views[k]["text"])
        kw[k] = -np.inf
        ranks_kw.append(_rank_of(np.maximum.reduceat(kw, starts), gold))
    systems = {"bm25": _summary(ranks_kw), "base": _summary(ranks_dense)} if held else {}
    faq_entries = sum(1 for e in index.entries if e.get("kind") == "faq")
    verdict = []
    if held:
        verdict.append(f"{len(held)} checks: each holds out one wording of an FAQ question and asks whether the others still find it ({len(q_by_entry) - single} entries have more than one wording).")
        verdict.append("“MiniLM, as downloaded” includes the questions you approved on the Test page. This engine searches with MiniLM as downloaded: it is not fine-tuned here (that runs on your own computer).")
        if single:
            verdict.append(f"{single} of {faq_entries} FAQ entries have only one wording, so they cannot be checked yet: add alternative questions, or approve questions on the Test page.")
    else:
        verdict.append("Nothing to measure yet: no FAQ entry has more than one wording. Add alternative questions to your FAQ file, or approve questions on the Test page (“This answers it”): every approved question becomes a check.")
    return {"entries": n_entries, "views": len(views), "checks": len(held), "systems": systems, "training": None, "verdict": verdict,
            "model": model or {"base": "minilm-l6", "params_millions": 22, "dimension": int(V.shape[1]) if V.size else 384}, "tuned": False}
