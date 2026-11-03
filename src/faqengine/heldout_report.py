"""Scores keyword search, the unchanged base model and the trained model on the independent held-out questions.

Three rules keep it honest:
  * the dev questions choose everything that is chosen (when to stop, which model, where the refusal line goes); the test
    questions only report, and no figure for the shipped model is tuned on them;
  * "training improved things" is claimed only when the paired difference on the test questions excludes zero;
    the trained model becomes the default only if it won on dev and is not worse on test, otherwise the base stays;
  * refusing out-of-scope questions is validated separately from retrieval, near-topic and unrelated questions apart.
"""
from __future__ import annotations

import copy
import time

import numpy as np
import torch

from . import metrics
from .bm25 import bm25_entry_scores
from .encoder import Encoder
from .evaluate import SCALE, Scorer
from .heldout import SINGLE_TYPES, IN_SCOPE_TYPES, LABELS, OOS_TYPES, HeldOut
from .kb import KB
from .report import NAMES as SYSTEM_NAMES
from .report import entry_map, faq_health, faq_vectors, source_summary

SYSTEMS = ("bm25", "base", "tuned")
METRICS = ("hit@1", "p@3", "r@3", "p@5", "r@5", "mrr@10", "ndcg@10")
GRID = [round(float(x), 2) for x in np.arange(0.15, 0.701, 0.05)]
MISS_WEIGHT = 2.0  # wrongly refusing a real question counts double against wrongly answering an off-topic one


def score_matrices(kb: KB, queries: list[str], base: Encoder, tuned: Encoder, device: torch.device) -> dict[str, np.ndarray]:
    return {"bm25": bm25_entry_scores(kb, queries), "base": Scorer(base, kb, device).scores(queries), "tuned": Scorer(tuned, kb, device).scores(queries)}


def typed_metrics(mats: dict, ho: HeldOut, split: str) -> tuple[dict, dict]:
    """(table, per-question arrays) for the in-scope questions of one split, overall and by type."""
    rows = ho.where(split, IN_SCOPE_TYPES)
    if not rows:
        return {"n": 0, "overall": {}, "by_type": {}}, {}
    gold = [ho.gold[i] for i in rows]
    types = np.array([ho.types[i] for i in rows])
    pq = {s: metrics.per_query_sets(mats[s][rows], gold) for s in SYSTEMS}
    mean = lambda d, m=slice(None): {k: float(d[k][m].mean()) for k in METRICS}  # noqa: E731
    table = {"n": len(rows), "overall": {s: mean(pq[s]) for s in SYSTEMS}, "by_type": {}}
    for t in IN_SCOPE_TYPES:
        m = types == t
        if m.any():
            table["by_type"][t] = {"n": int(m.sum()), **{s: mean(pq[s], m) for s in SYSTEMS}}
    return table, pq


MIN_DEV_OOS = 8  # fewer out-of-scope dev questions than this cannot place the refusal line on their own


def choose_floor(top_in: np.ndarray, top_oos: np.ndarray, probe_tops: np.ndarray | None = None) -> float:
    """The refusal line from dev questions: maximise (off-topic refused) - MISS_WEIGHT * (real questions refused).

    With only a handful of out-of-scope dev questions the best line on them is luck, so then the generic off-topic questions that this knowledge base
    does not itself cover (`probe_tops`) are added to them, and the line may never be above the 10th percentile of the right matches' scores:
    showing a candidate for review costs little, refusing a real question is the worst outcome."""
    few = len(top_oos) < MIN_DEV_OOS
    if few and probe_tops is not None and len(probe_tops):
        top_oos = np.concatenate([top_oos, probe_tops])
    if len(top_oos) == 0 or len(top_in) == 0:
        return float(np.quantile(top_in, 0.05)) if len(top_in) else 0.0
    best_t, best_j = 0.0, -np.inf
    for t in np.unique(np.round(np.concatenate([top_in, top_oos]), 3)):
        j = float((top_oos < t).mean()) - MISS_WEIGHT * float((top_in < t).mean())
        if j > best_j + 1e-12:
            best_t, best_j = float(t), j
    if few and len(top_in) >= 8:
        best_t = min(best_t, float(np.quantile(top_in, 0.10)))
    return best_t


def refusal_report(mats: dict, ho: HeldOut, floors: dict | None = None, probes: dict | None = None) -> dict:
    """Does it say "not in the FAQ" when it should, and only then? Chosen on dev, reported on test, near and unrelated apart."""
    floors = floors or {}
    dev_in, dev_oos = ho.where("dev", IN_SCOPE_TYPES), ho.where("dev", OOS_TYPES)
    te_in, near, far = ho.where("test", IN_SCOPE_TYPES), ho.where("test", ("out-of-scope-near",)), ho.where("test", ("out-of-scope-far",))
    other = ho.where("test", ("out-of-scope",))  # out of scope, not said whether near the topic or unrelated
    out = {}
    for s in ("base", "tuned"):
        top = mats[s].max(1)
        manual = floors.get(s) is not None
        floor = float(floors[s]) if manual else choose_floor(top[dev_in], top[dev_oos], (probes or {}).get(s))
        helped = not manual and probes is not None and len(dev_oos) < MIN_DEV_OOS
        rate = lambda rows, t=None: float((top[rows] < (floor if t is None else t)).mean()) if rows else None  # noqa: E731
        out[s] = {
            "floor": floor, "floor_source": "manual" if manual else ("dev questions plus generic off-topic questions (there were few of your own)" if helped else "dev questions"),
            "dev": {"in_scope_refused": rate(dev_in), "off_topic_refused": rate(dev_oos), "n_in_scope": len(dev_in), "n_off_topic": len(dev_oos)},
            "test": {"in_scope_refused": rate(te_in), "off_topic_near_refused": rate(near), "off_topic_far_refused": rate(far),
                     "off_topic_other_refused": rate(other), "n_other": len(other), "auroc_other": metrics.auroc(top[te_in], top[other]) if other and te_in else None,
                     "n_in_scope": len(te_in), "n_near": len(near), "n_far": len(far),
                     "auroc_near": metrics.auroc(top[te_in], top[near]) if near and te_in else None,
                     "auroc_far": metrics.auroc(top[te_in], top[far]) if far and te_in else None},
            "sweep": [{"floor": t, "in_scope_refused": rate(te_in, t), "off_topic_near_refused": rate(near, t), "off_topic_far_refused": rate(far, t)} for t in GRID],
        }
    return out


def training_effect(pq_base: dict, pq_tuned: dict) -> dict:
    out = {"n": int(len(pq_base["hit@1"]))}
    for key in ("hit@1", "mrr@10", "ndcg@10"):
        diff, lo, hi = metrics.paired_bootstrap(pq_base[key], pq_tuned[key], n=4000)
        out[key] = {"base": float(pq_base[key].mean()), "tuned": float(pq_tuned[key].mean()), "diff": diff, "low": lo, "high": hi}
    out["worse"] = bool(out["hit@1"]["diff"] < 0 or out["mrr@10"]["diff"] < 0)
    out["demonstrated"] = bool(not out["worse"] and out["hit@1"]["low"] > 0)  # the headline number must clear chance by itself
    out["ranking_better"] = bool(not out["worse"] and out["mrr@10"]["low"] > 0)  # a supporting signal, never enough to claim the improvement
    return out


def decide(beat_on_dev: bool, effect: dict | None) -> dict:
    """Which model is the default. The decision uses the validation questions only (`beat_on_dev` is the training run's verdict on them): held-out test
    questions are never used to choose, so what they show can be reported honestly. They decide only whether an improvement may be CLAIMED."""
    ship = beat_on_dev
    if not beat_on_dev:
        why = "no checkpoint beat the pretrained model on the validation questions, so the pretrained model is kept"
    elif effect is None:
        why = "the fine-tuned model won on the validation questions; there are no held-out test questions to confirm it"
    elif effect["demonstrated"]:
        why = "the fine-tuned model won on the validation questions, and the held-out test confirms it beyond chance"
    elif effect["worse"]:
        why = ("the fine-tuned model won on the validation questions but is worse than the pretrained model on the held-out test. It stays the default because test questions "
               "are never used to choose; switch fine-tuning off on the Files tab to use the pretrained model")
    elif effect.get("ranking_better"):
        why = ("the fine-tuned model won on the validation questions; on the held-out test its ranking (MRR@10) is better beyond chance, but right-entry-first (Hit@1) is not, "
               "so a Hit@1 improvement is NOT claimed")
    else:
        why = "the fine-tuned model won on the validation questions, but the held-out test shows no improvement beyond chance, so none is claimed"
    return {"ship": "tuned" if ship else "base", "reason": why, "improvement_demonstrated": bool(ship and effect and effect["demonstrated"])}


def answering_eval(kb: KB, ho: HeldOut, scores: np.ndarray, split: str = "test", sim=None, sem_min: float | None = None, n: int = 8) -> dict:
    """The evidence-aware answer (runtime/evidence.py) for every held-out test question, judged against what was expected: an answer that cites an expected
    entry is right, an answer to a question that should be abstained from is a fabrication, a refusal of an answerable question is a missed answer, and a stored
    FAQ answer given where only a document holds the information is a misuse. Hits are built from the scores alone (the matched wording is the entry's question)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "runtime"))
    import evidence

    rows = ho.where(split)
    tally: dict[str, dict] = {}
    extra = {} if sem_min is None else {"sim": sim, "sem_min": sem_min}
    for i in rows:
        order = np.argsort(-scores[i])
        faq, passages = [], []
        for j in order:
            e = kb.entries[int(j)]
            bucket = faq if e.kind == "faq" else passages
            if len(bucket) < n:
                bucket.append({"rank": len(bucket) + 1, "entry_id": e.id, "question": e.question, "answer": e.answer, "kind": e.kind, "source": e.source,
                               "score": float(scores[i, j]), "matched": e.question, "matched_kind": "q"})
            if len(faq) >= n and len(passages) >= n:
                break
        r = evidence.respond(ho.queries[i], faq, passages, **extra)
        gold = {kb.entries[g].id for g in ho.gold[i]}
        cited = {x["id"] for x in r.get("sources", [])}
        answered = r["outcome"] in ("faq", "grounded")
        oos = ho.types[i] in OOS_TYPES
        faq_only_passages = not oos and all(kb.entries[g].kind == "passage" for g in ho.gold[i])
        verdict = ("fabricated answer" if oos and r["outcome"] == "partial" else "partial answer" if r["outcome"] == "partial" else "correct abstention" if oos and not answered else "fabricated answer" if oos else "answered correctly" if answered and cited & gold
                   else "stored FAQ answer for a document question" if r["outcome"] == "faq" and faq_only_passages else "answered from the wrong source" if answered else "missed answer")
        for key in ("all", ho.types[i]):
            t = tally.setdefault(key, {"n": 0, "outcomes": {}, "verdicts": {}})
            t["n"] += 1
            t["outcomes"][r["outcome"]] = t["outcomes"].get(r["outcome"], 0) + 1
            t["verdicts"][verdict] = t["verdicts"].get(verdict, 0) + 1
    return tally


SEM_GRID = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 1.01)  # 1.01: the words alone decide


def make_sim(enc: Encoder, device: torch.device):
    """sim(part, texts): cosine between a question part and each text, by the model that ships (cached: the same pairs come up for every setting tried)."""
    cache: dict = {}

    def sim(part: str, texts: list[str]) -> list[float]:
        new = [t for t in texts if (part, t) not in cache]
        if new:
            q = enc.encode([part], "query", device=device)[0]
            for t, v in zip(new, enc.encode(new, "doc", device=device) @ q):
                cache[(part, t)] = float(v)
        return [cache[(part, t)] for t in texts]
    return sim


def choose_sem_min(kb: KB, ho: HeldOut, scores: np.ndarray, sim) -> tuple[float, dict]:
    """The similarity a sentence needs to count as supporting a question, chosen on the VALIDATION questions (right answers and right abstentions up,
    fabricated answers and answers from the wrong source heavily down), never on the held-out test questions."""
    if not ho.where("dev"):
        return 0.45, {}
    best, scored = None, {}
    for m in SEM_GRID:
        t = answering_eval(kb, ho, scores, "dev", sim, m)["all"]
        v = t["verdicts"]
        scored[m] = (v.get("answered correctly", 0) + v.get("correct abstention", 0) - 3 * v.get("fabricated answer", 0) - v.get("answered from the wrong source", 0)) / t["n"]
        if best is None or (scored[m], -abs(m - 0.45)) > (scored[best], -abs(best - 0.45)):  # a tie (the validation questions cannot tell) keeps the value nearest the prior, 0.45
            best = m
    return best, {str(k): round(v, 3) for k, v in scored.items()}


ALPHAS = (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0)  # the share of keyword score in the blend; 0 is the model alone, 1 is keywords alone


def _unit(m: np.ndarray) -> np.ndarray:
    """Each question's scores over the entries stretched to 0..1, so a model score and a keyword score can be added."""
    lo, hi = m.min(1, keepdims=True), m.max(1, keepdims=True)
    return (m - lo) / np.maximum(hi - lo, 1e-9)


def hybrid_scores(dense: np.ndarray, keyword: np.ndarray, alpha: float) -> np.ndarray:
    return (1 - alpha) * _unit(dense) + alpha * _unit(keyword)


def hybrid_experiment(mats: dict, ho: HeldOut, shipped: str) -> dict:
    """Keyword search against the model against both together, on the same held-out test questions. The blend (alpha) is chosen on the validation questions
    by MRR@10, never on the test questions. An experiment: the engine itself still ranks with the model alone."""
    dense, keyword = mats[shipped], mats["bm25"]
    val = ho.where("dev", IN_SCOPE_TYPES)
    if not val or not ho.where("test", IN_SCOPE_TYPES):
        return {"available": False, "reason": "needs validation and held-out test questions"}
    gold = [ho.gold[i] for i in val]
    mrr = {a: float(metrics.per_query_sets(hybrid_scores(dense[val], keyword[val], a), gold)["mrr@10"].mean()) for a in ALPHAS}
    alpha = max(ALPHAS, key=lambda a: (round(mrr[a], 6), -a))  # best on validation; a tie keeps the smaller share of keywords
    fused = hybrid_scores(dense, keyword, alpha)
    table, pq = typed_metrics({"bm25": keyword, "base": dense, "tuned": fused}, ho, "test")
    rename = {"bm25": "keyword", "base": "embedding", "tuned": "hybrid"}
    cut = lambda d: {rename[k]: v for k, v in d.items() if k in rename}  # noqa: E731
    eff = {}
    for key in ("hit@1", "mrr@10"):
        diff, lo, hi = metrics.paired_bootstrap(pq["base"][key], pq["tuned"][key], n=4000)
        eff[key] = {"embedding": float(pq["base"][key].mean()), "hybrid": float(pq["tuned"][key].mean()), "diff": diff, "low": lo, "high": hi}
    h = eff["hit@1"]
    verdict = ("Hybrid retrieval beats the model alone beyond chance" if h["low"] > 0 else "Hybrid retrieval is worse than the model alone beyond chance" if h["high"] < 0
               else "Hybrid retrieval is not distinguishable from the model alone")
    return {"available": True, "alpha": alpha, "validation_mrr": {str(a): v for a, v in mrr.items()}, "overall": cut(table["overall"]),
            "by_type": {t: {"n": d["n"], **cut(d)} for t, d in table["by_type"].items()}, "effect": eff, "n": table["n"],
            "verdict": f"{verdict} on the {table['n']} held-out test questions (right first {_pct(h['embedding'])} → {_pct(h['hybrid'])}, 95% interval {100 * h['low']:+.1f} to {100 * h['high']:+.1f} points; "
                       f"keyword share {alpha:g}, chosen on the validation questions). Experiment only: the engine still ranks with the model alone."}


def evaluate_heldout(kb: KB, ho: HeldOut, base: Encoder, tuned: Encoder, beat_on_dev: bool, device: torch.device,
                     floor: float | None = None) -> tuple[dict, dict]:
    """The `heldout` section of a report, and the score matrices (for callers that need per-question arrays)."""
    mats = score_matrices(kb, ho.queries, base, tuned, device)
    if ho.val_kb is not None:  # validation questions are scored against the knowledge base without their own phrasings, never against the full index
        vm = score_matrices(ho.val_kb, ho.queries, base, tuned, device)
        rows = ho.where("dev")
        for k in mats:
            mats[k][rows] = vm[k][rows]
    dev_table, _ = typed_metrics(mats, ho, "dev")
    test_table, pq = typed_metrics(mats, ho, "test")
    effect = training_effect(pq["base"], pq["tuned"]) if pq else None
    decision = decide(beat_on_dev, effect) if effect else {"ship": "base", "reason": "no test questions", "improvement_demonstrated": False}
    shipped = decision["ship"]
    floors = {shipped: floor} if floor is not None else None
    probes = None
    if len(ho.where("dev", OOS_TYPES)) < MIN_DEV_OOS:  # too few of the person's own out-of-scope questions to place the line: borrow generic ones this FAQ does not cover
        from .selfcheck import usable_probes

        texts = usable_probes(kb.entries)[0][:12]
        if texts:
            pm = score_matrices(kb, texts, base, tuned, device)
            probes = {k: pm[k].max(1) for k in ("base", "tuned")}
    refusal = refusal_report(mats, ho, floors, probes)
    single = [i for i in ho.where("test", SINGLE_TYPES) if len(ho.gold[i]) == 1]  # questions with exactly one right entry: the loss needs one
    loss = {k: metrics.listwise_loss(mats[k][single], np.array([next(iter(ho.gold[i])) for i in single], dtype=int), SCALE) for k in ("base", "tuned")} if single else {}
    hybrid = hybrid_experiment(mats, ho, shipped)
    enc = tuned if shipped == "tuned" else base
    sim = make_sim(enc, device)
    sem_min, sem_scores = choose_sem_min(kb, ho, mats[shipped], sim)
    answering = answering_eval(kb, ho, mats[shipped], "test", sim, sem_min) if ho.where("test") else {}
    section = {"hybrid": hybrid, "answering": answering, "evidence_sim_min": sem_min, "evidence_sim_validation": sem_scores, "counts": ho.file_counts if hasattr(ho, "file_counts") else ho.counts(), "removed": ho.removed, "warnings": ho.warnings, "dev": dev_table, "test": test_table, "effect": effect,
               "decision": decision, "refusal": refusal, "loss_test": loss, "labels": LABELS, "names": SYSTEM_NAMES,
               "owner_typed": sum(1 for s in ho.source if s == "owner")}
    return section, mats


def build_heldout_report(kb: KB, ho: HeldOut, base: Encoder, tuned: Encoder, log: dict, device: torch.device, train_seconds: float,
                         peak_mem_mb: float, hw: dict | None, floor: float | None = None, direct: float | None = None) -> tuple[dict, dict, np.ndarray, Encoder]:
    """Everything the studio's Results page shows for a project that has held-out questions. Returns (report, thresholds, vectors, shipped encoder)."""
    section, mats = evaluate_heldout(kb, ho, base, tuned, bool(log.get("beat_untuned_on_dev")), device, floor)
    shipped_name = section["decision"]["ship"]
    shipped = tuned if shipped_name == "tuned" else base
    cpu = torch.device("cpu")
    served = Scorer(copy.deepcopy(shipped).to(cpu), kb, cpu)
    sample = ho.queries[:40]
    times = []
    for t in sample:
        t0 = time.perf_counter()
        served.scores([t])
        times.append((time.perf_counter() - t0) * 1000)
    V = served.V
    faq_kb, V_faq = faq_vectors(kb, V)
    params = sum(p.numel() for p in shipped.parameters())
    rf = section["refusal"][shipped_name]
    thresholds = {"floor": rf["floor"], "direct": direct, "evidence_sim_min": section["evidence_sim_min"], "source": "manual" if (floor is not None or direct is not None) else "held-out dev questions",
                  "direct_reason": None if direct is not None else "answering without an LLM is off until enough of your own tests prove it safe"}
    report = {
        "mode": "heldout",
        "faq": {"entries": len(faq_kb) if faq_kb else 0, "passages": len(kb) - (len(faq_kb) if faq_kb else 0), "sources": source_summary(kb), "views": len(kb.views),
                "phrasings": sum(len(e.questions) for e in faq_kb.entries) if faq_kb else 0,
                "health": faq_health(faq_kb, V_faq) if faq_kb else {"entries": 0, "single_phrasing": 0, "confusable": [], "advice": list(kb.warnings)}},
        "heldout": section,
        "training": {k: v for k, v in log.items() if k != "curve"} | {"peak_memory_mb": peak_mem_mb, "train_seconds": train_seconds},
        "curve": log.get("curve", []),
        "model": {"base": shipped.spec.key, "hf_id": shipped.spec.hf_id, "license": shipped.spec.license, "params_millions": round(params / 1e6, 1),
                  "size_fp32_mb": round(params * 4 / 2**20, 1), "dimension": shipped.dim, "vectors": len(kb.views), "vectors_mb": round(V.nbytes / 2**20, 3),
                  "latency_ms_p50": float(np.percentile(times, 50)), "latency_ms_p95": float(np.percentile(times, 95)),
                  "latency_note": "one question at a time on this machine's CPU with PyTorch (the exported ONNX engine is faster)",
                  "trained_on": str(device), "tuned": shipped_name == "tuned"},
        "map": entry_map(faq_kb, V_faq) if faq_kb else None, "thresholds": thresholds, "hardware": hw or {}, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    report["verdict"] = heldout_verdict(report)
    return report, thresholds, V, shipped


def _f2(x) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def heldout_verdict(r: dict) -> list[str]:
    h = r["heldout"]
    t, eff, dec = h["test"], h["effect"], h["decision"]
    out = []
    if eff:
        e = eff["hit@1"]
        if dec["improvement_demonstrated"]:
            out.append(f"Training improved the engine on held-out questions: right entry first {_pct(e['base'])} → {_pct(e['tuned'])} "
                       f"({100 * e['diff']:+.1f} points, 95% interval {100 * e['low']:+.1f} to {100 * e['high']:+.1f}, {eff['n']} questions).")
        elif dec["ship"] == "tuned":
            m = eff["mrr@10"]
            out.append(f"The trained model is the default, but a right-entry-first improvement is not demonstrated: {_pct(e['base'])} → {_pct(e['tuned'])} "
                       f"(95% interval {100 * e['low']:+.1f} to {100 * e['high']:+.1f} points, which includes no change)."
                       + (f" Its ranking (MRR@10) is better beyond chance: {m['base']:.3f} → {m['tuned']:.3f} (95% interval {m['low']:+.3f} to {m['high']:+.3f})." if eff.get("ranking_better") else ""))
        else:
            out.append(f"The unchanged base model is the default. {dec['reason'][0].upper() + dec['reason'][1:]}. "
                       f"Held-out right entry first: base {_pct(e['base'])}, trained {_pct(e['tuned'])}.")
    ov = t["overall"]
    out.append(f"On {t['n']} held-out test questions the right entry came first {_pct(ov['tuned']['hit@1'])} of the time "
               f"(base model {_pct(ov['base']['hit@1'])}, keyword search {_pct(ov['bm25']['hit@1'])}).")
    kw, shipped = ov["bm25"]["hit@1"], ov[dec["ship"]]["hit@1"]
    if t["n"] >= 10 and kw >= shipped + 0.07:
        out.append(f"Keyword search found the right entry first more often than the model did ({_pct(kw)} against {_pct(shipped)}). That usually means the model does not understand some of "
                   "your text (another language or alphabet, product codes, names, abbreviations): check the language on the Files tab. Searching with keywords and the model together is not built yet.")
    weakest = min(((k, v) for k, v in t["by_type"].items() if k != "ambiguous"), key=lambda kv: kv[1]["tuned"]["hit@1"], default=None)
    if weakest:
        out.append(f"Weakest kind of question: {weakest[0]} ({_pct(weakest[1]['tuned']['hit@1'])} right first, {weakest[1]['n']} questions).")
    rf = h["refusal"][dec["ship"]]["test"]
    out.append(f"Refusal, validated separately: {_pct(rf['off_topic_near_refused'])} of near-topic and {_pct(rf['off_topic_far_refused'])} of unrelated out-of-scope "
               f"questions refused; {_pct(rf['in_scope_refused'])} of answerable questions wrongly refused.")
    if h["counts"]["removed_for_leakage"]:
        out.append(f"{h['counts']['removed_for_leakage']} held-out questions were removed because they were near-duplicates of training phrasings.")
    out.append("The held-out questions were written separately from the training phrasings, not by customers and not generated from training examples. "
               "Real customer questions remain the best test: use the Test page.")
    return out


def render_heldout_markdown(r: dict) -> str:
    h, m, th = r["heldout"], r["model"], r["thresholds"]
    names = SYSTEM_NAMES
    L = ["# Engine report (held-out evaluation)", ""] + [f"- {v}" for v in r["verdict"]]
    for split in ("test", "dev"):
        t = h[split]
        if not t["n"]:
            continue
        L += ["", f"## {split.capitalize()} questions: all in-scope types ({t['n']} questions)" + ("  (reported)" if split == "test" else "  (used to choose, not to report)"), "",
              "| System | Hit@1 | P@3 | R@3 | P@5 | R@5 | MRR@10 | nDCG@10 |", "|---|---|---|---|---|---|---|---|"]
        for s in SYSTEMS:
            x = t["overall"][s]
            L.append(f"| {names[s]} | {_pct(x['hit@1'])} | {_pct(x['p@3'])} | {_pct(x['r@3'])} | {_pct(x['p@5'])} | {_pct(x['r@5'])} | {x['mrr@10']:.3f} | {x['ndcg@10']:.3f} |")
    t = h["test"]
    L += ["", "## Test questions by type (side by side)", "", "| Type | n | System | Hit@1 | P@3 | R@3 | P@5 | R@5 | MRR@10 | nDCG@10 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for ty, d in t["by_type"].items():
        for i, s in enumerate(SYSTEMS):
            x = d[s]
            L.append(f"| {h['labels'][ty] if i == 0 else ''} | {d['n'] if i == 0 else ''} | {names[s]} | {_pct(x['hit@1'])} | {_pct(x['p@3'])} | {_pct(x['r@3'])} | {_pct(x['p@5'])} | {_pct(x['r@5'])} | {x['mrr@10']:.3f} | {x['ndcg@10']:.3f} |")
    eff = h["effect"]
    if eff:
        L += ["", "## Did training help? (trained vs the unchanged base model, test questions)", "", "| Metric | Base | Trained | Difference | 95% interval |", "|---|---|---|---|---|"]
        for k, lab in (("hit@1", "Hit@1"), ("mrr@10", "MRR@10"), ("ndcg@10", "nDCG@10")):
            e = eff[k]
            f = (lambda v: f"{100 * v:.1f}%") if k == "hit@1" else (lambda v: f"{v:.3f}")
            L.append(f"| {lab} | {f(e['base'])} | {f(e['tuned'])} | {e['diff'] * (100 if k == 'hit@1' else 1):+.{1 if k == 'hit@1' else 3}f}{' pts' if k == 'hit@1' else ''} | "
                     f"{e['low'] * (100 if k == 'hit@1' else 1):+.{1 if k == 'hit@1' else 3}f} to {e['high'] * (100 if k == 'hit@1' else 1):+.{1 if k == 'hit@1' else 3}f} |")
        L += ["", f"Default model: **{h['decision']['ship']}**. {h['decision']['reason'][0].upper() + h['decision']['reason'][1:]}."]
    L += ["", "## Refusal behaviour (validated separately)", "",
          "The \"not in the FAQ\" line is chosen on validation questions only. Test questions report it. Near-topic = about your subject but not in the FAQ; unrelated = anything else.", "",
          "| Model | Line | Near-topic refused | Unrelated refused | Not labelled near/unrelated, refused | Answerable wrongly refused | AUROC near | AUROC unrelated |", "|---|---|---|---|---|---|---|---|"]
    for s in ("base", "tuned"):
        x = h["refusal"][s]
        te = x["test"]
        L.append(f"| {names[s]} | {x['floor']:.2f} ({x['floor_source']}) | {_pct(te['off_topic_near_refused'])} ({te['n_near']}) | {_pct(te['off_topic_far_refused'])} ({te['n_far']}) | "
                 f"{_pct(te['off_topic_other_refused'])} ({te['n_other']}) | {_pct(te['in_scope_refused'])} ({te['n_in_scope']}) | {_f2(te['auroc_near'])} | {_f2(te['auroc_far'])} |")
    sw = h["refusal"][h["decision"]["ship"]]["sweep"]
    L += ["", f"Threshold sweep for the default model ({names[h['decision']['ship']]}), test questions:", "", "| Line | Near-topic refused | Unrelated refused | Answerable wrongly refused |", "|---|---|---|---|"]
    L += [f"| {x['floor']:.2f} | {_pct(x['off_topic_near_refused'])} | {_pct(x['off_topic_far_refused'])} | {_pct(x['in_scope_refused'])} |" for x in sw]
    c = h["counts"]
    L += ["", "## Data checks", "", f"- Held-out questions: {c['total']} kept ({c['by_split']['dev']} dev, {c['by_split']['test']} test); {c['removed_for_leakage']} removed as near-duplicates of training phrasings.",
          f"- Searched: {r['faq']['entries']} FAQ entries ({r['faq']['phrasings']} phrasings)" + (f" and {r['faq']['passages']} document passages" if r['faq'].get('passages') else "") + f", {r['faq']['views']} searchable views; training uses the FAQ entries only."]
    L += [f"- Removed for leakage: “{x['query']}” (≈ “{x['matches_training_phrasing']}”)" for x in h["removed"]]
    L += ["", "## Engine", "", f"- Default model: `{m['hf_id']}` {'(fine-tuned on this FAQ)' if m['tuned'] else '(unchanged)'}, {m['params_millions']}M parameters, {m['size_fp32_mb']} MB",
          f"- \"Not in the FAQ\" below a score of {th['floor']:.2f} ({th['source']}); answering without an LLM: {'off' if th['direct'] is None else 'from ' + format(th['direct'], '.2f')}",
          f"- Latency {m['latency_ms_p50']:.1f} ms median ({m['latency_note']})"]
    if r["faq"]["health"]["advice"]:
        L += ["", "## FAQ health", ""] + [f"- {a}" for a in r["faq"]["health"]["advice"]]
    return "\n".join(L) + "\n"
