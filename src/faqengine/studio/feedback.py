"""What a person's approvals and rejections are worth: a tally, extra training phrasings, and evidence for the thresholds."""
from __future__ import annotations

import math

import numpy as np

from ..evaluate import certify

MIN_LABELED = 20  # fewer judged tests than this cannot certify answering without an LLM


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    d = 1 + z * z / n
    return max(0.0, (centre - spread) / d), min(1.0, (centre + spread) / d)


def tally(history: list[dict]) -> dict:
    """Accuracy so far. A test that matched a question the person had already approved proves nothing new (the engine was told the
    answer), so it is counted as tested but not as right or wrong."""
    fresh = [h for h in history if not h.get("learned")]
    approved = sum(h.get("verdict") == "approve" for h in fresh)
    rejected = sum(h.get("verdict") == "reject" for h in fresh)
    labeled = approved + rejected
    lo, hi = wilson_interval(approved, labeled)
    return {"tested": len(history), "approved": approved, "rejected": rejected, "unjudged": len(fresh) - labeled, "from_learned": len(history) - len(fresh),
            "right": approved / labeled if labeled else None, "right_low": lo if labeled else None, "right_high": hi if labeled else None}


def label_rows(history: list[dict]) -> list[tuple[float, bool]]:
    """(top score, was the engine's top answer right and in scope) for every judged test.

    Approve: the answer, or "not in the FAQ", was right. Reject with the right entry named: the top answer was right only if it
    was that entry. Reject with "none" (or nothing named): answering at all was wrong. A rejected "not in the FAQ" with no entry
    named says nothing useful, so it is left out."""
    rows = []
    for h in history:
        v, band, top = h.get("verdict"), h.get("band"), h.get("top_score")
        if v not in ("approve", "reject") or top is None or h.get("learned"):  # a learned match is a memorised answer, not evidence
            continue
        ce = h.get("correct_entry")
        if v == "approve":
            rows.append((top, band != "none"))
        elif ce in (None, ""):
            if band != "none":
                rows.append((top, False))
        elif ce == "none":
            rows.append((top, False))
        else:
            rows.append((top, band != "none" and ce == h.get("top_entry")))
    return rows


def recalibrate(history: list[dict], current: dict, target: float = 0.95) -> dict:
    """Thresholds after taking a person's judgements into account.

    direct: answering without an LLM is allowed only once enough judged tests prove it (the same lower-confidence-bound test used
    elsewhere, with every wrongly answered test, including off-topic ones, counted against it).
    floor: never raised (a person's tests may be easy), but lowered if correct matches were found below it."""
    th = dict(current)
    th["target_precision"] = target
    rows = label_rows(history)
    th["labeled"] = len(rows)
    if not rows:
        return th
    top = np.array([r[0] for r in rows])
    ok = np.array([r[1] for r in rows])
    if len(rows) >= MIN_LABELED:
        th["direct"], reason = certify(top, ok, target)
        th["direct_reason"] = reason
        th["source"] = f"your tests ({len(rows)})"
    else:
        th["direct"] = None
        th["direct_reason"] = f"needs {MIN_LABELED - len(rows)} more judged tests to prove it is safe"
    if ok.sum() >= 3:
        floor = min(th["floor"], float(np.quantile(top[ok], 0.05)))
        th["floor"] = min(floor, th["direct"]) if th.get("direct") is not None else floor
    return th


def extra_phrasings(history: list[dict], entry_ids: set[str]) -> dict[str, list[str]]:
    """Questions a person confirmed belong to an entry, to be trained on and indexed in the next run."""
    out: dict[str, list[str]] = {}
    for h in history:
        v, band, q = h.get("verdict"), h.get("band"), h.get("query")
        if not q:
            continue
        if v == "approve" and band != "none" and h.get("top_entry") in entry_ids:
            target = h["top_entry"]
        elif v == "reject" and h.get("correct_entry") in entry_ids:
            target = h["correct_entry"]
        else:
            continue
        if q.lower() not in {x.lower() for x in out.setdefault(target, [])}:
            out[target].append(q)
    return out
