"""Compare the tuned model with its baselines, calibrate the answer thresholds, and write the report."""
from __future__ import annotations

import json
import math
import platform
from pathlib import Path

import numpy as np
import torch

from . import metrics
from .bm25 import bm25_entry_scores
from .encoder import Encoder, pick_device
from .kb import KB, Queries

LABELS = {
    "bm25": "BM25 (keywords)",
    "base": "Untuned model",
    "tuned": "Tuned model",
    "hybrid_base": "Untuned + BM25 (weight set on dev)",
    "hybrid_tuned": "Tuned + BM25 (weight set on dev)",
}
SCALE = 20.0


class Scorer:
    """Scores questions against every entry of one knowledge base with one encoder."""

    def __init__(self, enc: Encoder, kb: KB, device: torch.device):
        self.enc, self.kb, self.device = enc.to(device), kb, device
        self.V = enc.encode([v.text for v in kb.views], "doc", device=device)

    def scores(self, texts: list[str]) -> np.ndarray:
        return self.kb.entry_max(self.enc.encode(texts, "query", device=self.device) @ self.V.T)


FUSION_WEIGHTS = (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0)


def fuse(dense: np.ndarray, lexical: np.ndarray, w: float) -> np.ndarray:
    """Cosine plus w times BM25 scaled to 0..1 per question."""
    peak = lexical.max(1, keepdims=True)
    return dense + w * np.divide(lexical, peak, out=np.zeros_like(lexical), where=peak > 0)


def pick_weight(dense: np.ndarray, lexical: np.ndarray, gold: np.ndarray) -> float:
    """The BM25 weight that does best on dev; 0 (no BM25) wins ties, so the hybrid is never worse than its dense half on dev."""
    return max(FUSION_WEIGHTS, key=lambda w: metrics.summarize(fuse(dense, lexical, w), gold)["mrr@10"])


def wilson_lower(correct: np.ndarray, n: np.ndarray, z: float = 1.28) -> np.ndarray:
    """One-sided 90% lower confidence bound on a proportion."""
    p = correct / n
    centre = p + z * z / (2 * n)
    spread = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


def certify(top: np.ndarray, ok: np.ndarray, target_precision: float) -> tuple[float | None, str | None]:
    """The lowest score at which answering is right at least `target_precision` of the time, judged by the lower 90%
    confidence bound (so a lucky sample cannot set the line). (None, reason) when the evidence is not there."""
    order = np.argsort(-top)
    answered = np.arange(1, len(order) + 1)
    enough = answered >= max(5, int(0.05 * len(order)))
    right = np.cumsum(ok[order])
    safe = np.flatnonzero((wilson_lower(right, answered) >= target_precision) & enough)
    if len(safe):
        return float(top[order][safe.max()]), None
    if np.any((right / answered >= target_precision) & enough):
        return None, "too few questions to certify the target precision"
    return None, "scores do not separate right answers from wrong ones well enough"


def calibrate(S: np.ndarray, gold: np.ndarray, target_precision: float = 0.95, floor_keep: float = 0.95) -> dict:
    """Pick the two thresholds from dev questions only.

    direct: see `certify`. It can come out as None: too few dev questions, or scores that do not separate.
    floor: below this the engine says it does not know. It is set so that `floor_keep` of the dev questions the
    engine gets right stay above it, because a wrongly refused customer costs more than a question the LLM band
    can decline. How well that floor rejects questions outside the knowledge base is measured, not assumed:
    `dev_simulated_oos_rejected` deletes each question's right entry and counts how many then fall below it.
    """
    rows = np.flatnonzero(gold >= 0)
    S, g = S[rows], gold[rows]
    top, ok = S.max(1), S.argmax(1) == g
    direct, reason = certify(top, ok, target_precision)
    floor = float(np.quantile(top[ok], 1 - floor_keep)) if ok.any() else 0.0
    if direct is not None:
        floor = min(floor, direct)
    loo = np.where(np.eye(S.shape[1], dtype=bool)[g], -np.inf, S).max(1)
    return {
        "direct": direct,
        "direct_reason": reason,
        "floor": floor,
        "target_precision": target_precision,
        "floor_keep": floor_keep,
        "dev_queries": int(len(g)),
        "dev_direct_share": float((top >= direct).mean()) if direct is not None else 0.0,
        "dev_simulated_oos_rejected": float((loo < floor).mean()),
    }


def bands(S: np.ndarray, gold: np.ndarray, th: dict) -> dict:
    """What the thresholds do on unseen questions, including questions the knowledge base cannot answer."""
    direct = th["direct"] if th["direct"] is not None else math.inf
    top, pred = S.max(1), S.argmax(1)
    inscope, oos = gold >= 0, gold < 0
    band = np.where(top >= direct, 2, np.where(top >= th["floor"], 1, 0))
    out: dict[str, float] = {}
    for name, b in (("direct", 2), ("llm", 1), ("none", 0)):
        sel = inscope & (band == b)
        out[f"in_{name}_share"] = float(sel.sum() / max(1, inscope.sum()))
        out[f"in_{name}_acc"] = float((pred[sel] == gold[sel]).mean()) if sel.any() else math.nan
        out[f"oos_{name}_share"] = float((oos & (band == b)).sum() / oos.sum()) if oos.any() else math.nan
    answered = band == 2
    right = answered & inscope & (pred == gold)
    out["direct_precision"] = float(right.sum() / answered.sum()) if answered.any() else math.nan
    out["direct_coverage"] = float(right.sum() / max(1, inscope.sum()))
    good = inscope & (pred == gold)
    out["auroc_vs_oos"] = metrics.auroc(top[good], top[oos]) if oos.any() else math.nan
    return out


def _train_queries(kb: KB) -> Queries:
    texts, gold = [], []
    for i, e in enumerate(kb.entries):
        texts += e.questions
        gold += [i] * len(e.questions)
    return Queries(texts, np.array(gold))


def evaluate(
    kb: KB,
    dev: Queries | None,
    test: Queries,
    base: Encoder,
    tuned: Encoder,
    train_log: dict | None = None,
    device: str = "auto",
    target_precision: float = 0.95,
) -> dict:
    dev_t = pick_device(device)
    s_base, s_tuned = Scorer(base, kb, dev_t), Scorer(tuned, kb, dev_t)
    mats = {"bm25": bm25_entry_scores(kb, test.texts), "base": s_base.scores(test.texts), "tuned": s_tuned.scores(test.texts)}
    weights = {"base": 0.1, "tuned": 0.1}  # without dev questions there is nothing to pick the weight on
    if dev is not None and len(dev):
        lex_dev = bm25_entry_scores(kb, dev.texts)
        weights = {"base": pick_weight(s_base.scores(dev.texts), lex_dev, dev.gold), "tuned": pick_weight(s_tuned.scores(dev.texts), lex_dev, dev.gold)}
    mats["hybrid_base"] = fuse(mats["base"], mats["bm25"], weights["base"])
    mats["hybrid_tuned"] = fuse(mats["tuned"], mats["bm25"], weights["tuned"])
    gold = test.gold
    pq = {name: metrics.per_query(m, gold) for name, m in mats.items()}
    systems = {name: {k: float(v.mean()) for k, v in d.items()} for name, d in pq.items()}

    lift = {}
    for a, b in (("base", "tuned"), ("bm25", "tuned"), ("hybrid_base", "hybrid_tuned"), ("hybrid_base", "tuned")):
        lift[f"{a}->{b}"] = {m: metrics.paired_bootstrap(pq[a][m], pq[b][m]) for m in ("hit@1", "mrr@10", "ndcg@10")}

    losses = {"test_base": metrics.listwise_loss(mats["base"], gold, SCALE), "test_tuned": metrics.listwise_loss(mats["tuned"], gold, SCALE)}
    if train_log:
        losses["train_tuned"] = train_log.get("train_loss_last")
    thresholds, abstention = {}, {}
    if dev is not None and len(dev):
        losses["dev_base"] = metrics.listwise_loss(s_base.scores(dev.texts), dev.gold, SCALE)
        losses["dev_tuned"] = metrics.listwise_loss(s_tuned.scores(dev.texts), dev.gold, SCALE)
        for name, scorer in (("base", s_base), ("tuned", s_tuned)):
            thresholds[name] = calibrate(scorer.scores(dev.texts), dev.gold, target_precision)
            abstention[name] = bands(mats[name], gold, thresholds[name])

    params = sum(p.numel() for p in tuned.parameters())
    return {
        "meta": {
            "entries": len(kb), "views": len(kb.views), "test_in_scope": int((gold >= 0).sum()), "test_out_of_scope": int((gold < 0).sum()),
            "dev_queries": len(dev) if dev is not None else 0, "base": tuned.spec.key, "hf_id": tuned.spec.hf_id, "license": tuned.spec.license,
            "params_millions": round(params / 1e6, 1), "size_fp32_mb": round(params * 4 / 2**20, 1), "device": str(dev_t),
            "torch": torch.__version__, "machine": f"{platform.machine()} {platform.system()}",
        },
        "systems": systems, "fusion_weights": weights, "lift": lift, "loss": losses, "thresholds": thresholds, "abstention": abstention,
        "training": {k: v for k, v in (train_log or {}).items() if k != "curve"}, "curve": (train_log or {}).get("curve", []),
        "warnings": kb.warnings,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def render_markdown(r: dict) -> str:
    m, t = r["meta"], r["training"]
    lines = [
        "# Evaluation report", "",
        f"- Knowledge base: {m['entries']} entries, {m['views']} searchable views",
        f"- Base model: `{m['hf_id']}` ({m['params_millions']}M parameters, {m['size_fp32_mb']} MB fp32, licence {m['license']})",
        f"- Unseen test questions: {m['test_in_scope']} answerable, {m['test_out_of_scope']} out of scope; dev questions used for stopping and thresholds: {m['dev_queries']}",
    ]
    if t:
        lines.append(f"- Training: {t['steps']} steps in {t['train_seconds']:.0f} s on {t['device']}; best checkpoint at step {t['best_step']}" +
                     ("" if t["beat_untuned_on_dev"] else " (no checkpoint beat the untuned model on dev, so the untuned weights are kept)"))
    lines += ["", "## Retrieval on unseen test questions", "",
              "| System | Hit@1 (=P@1=R@1) | P@3 | R@3 | P@5 | R@5 | MRR@10 | nDCG@10 |", "|---|---|---|---|---|---|---|---|"]
    for name, s in r["systems"].items():
        lines.append(f"| {LABELS[name]} | {_pct(s['hit@1'])} | {_pct(s['p@3'])} | {_pct(s['hit@3'])} | {_pct(s['p@5'])} | {_pct(s['hit@5'])} | {s['mrr@10']:.3f} | {s['ndcg@10']:.3f} |")
    lines += ["", "With one right entry per question, R@K equals Hit@K and P@K is Hit@K divided by K.", "",
              "## Lift (difference, 95% bootstrap interval over test questions)", "", "| Comparison | Hit@1 | MRR@10 | nDCG@10 |", "|---|---|---|---|"]
    for key, d in r["lift"].items():
        a, b = key.split("->")
        cells = [f"{v[0]:+.3f} [{v[1]:+.3f}, {v[2]:+.3f}]" for v in (d["hit@1"], d["mrr@10"], d["ndcg@10"])]
        lines.append(f"| {LABELS[a]} → {LABELS[b]} | " + " | ".join(cells) + " |")
    lines += ["", "An interval that includes 0 means the gain is not distinguishable from noise on this test set.", "",
              "## Loss (cross-entropy over entries; lower is better)", "", "| | Train (last window) | Dev | Test |", "|---|---|---|---|"]
    lo = r["loss"]
    lines.append(f"| Untuned | | {lo.get('dev_base', float('nan')):.3f} | {lo['test_base']:.3f} |")
    lines.append(f"| Tuned | {lo.get('train_tuned') or float('nan'):.3f} | {lo.get('dev_tuned', float('nan')):.3f} | {lo['test_tuned']:.3f} |")
    if r["abstention"]:
        lines += ["", "## When to answer, when to hand to the LLM, when to say \"I don't know\"", "",
                  "Thresholds are chosen on dev only. `direct` answers must be right at least "
                  f"{_pct(next(iter(r['thresholds'].values()))['target_precision'])} of the time (90% confidence bound), and the floor keeps "
                  f"{_pct(next(iter(r['thresholds'].values()))['floor_keep'])} of the dev questions answered correctly.", "",
                  "| Model | direct ≥ | floor ≥ | Direct precision, answerable questions only | Direct precision, out-of-scope counted wrong | Answerable questions answered directly | Out-of-scope kept off direct | Out-of-scope sent to \"don't know\" | AUROC answerable vs out-of-scope |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for name, a in r["abstention"].items():
            th = r["thresholds"][name]
            d = "never" if th["direct"] is None else f"{th['direct']:.3f}"
            kept = 1 - a["oos_direct_share"] if not math.isnan(a["oos_direct_share"]) else math.nan
            lines.append(f"| {LABELS[name]} | {d} | {th['floor']:.3f} | {_pct(a['in_direct_acc'])} | {_pct(a['direct_precision'])} | {_pct(a['direct_coverage'])} | {_pct(kept)} | {_pct(a['oos_none_share'])} | {a['auroc_vs_oos']:.3f} |")
    for name, th in r["thresholds"].items():
        if th["direct"] is None:
            lines += ["", f"{LABELS[name]}: no direct-answer threshold, because {th['direct_reason']}. Everything above the floor goes to the LLM."]
    rt = r.get("runtime")
    if rt:
        lines += ["", "## Exported engine", "", f"- Package size: {rt['package_mb']:.1f} MB; model file ({rt['onnx']}): {rt['model_mb']:.1f} MB",
                  f"- Query latency on CPU (tokenise, embed, search; one query at a time): median {rt['latency_ms_p50']:.1f} ms, p95 {rt['latency_ms_p95']:.1f} ms",
                  f"- Agreement with the PyTorch model: mean cosine {rt['parity_cosine_mean']:.5f}"]
    if r["warnings"]:
        lines += ["", "## Data warnings", ""] + [f"- {w}" for w in r["warnings"]]
    return "\n".join(lines) + "\n"


def save_report(r: dict, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(r, indent=2, default=lambda o: None if isinstance(o, float) and math.isnan(o) else o))
    (out / "report.md").write_text(render_markdown(r))
