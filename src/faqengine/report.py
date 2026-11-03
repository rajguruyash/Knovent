"""The report for a FAQ-only run: what the self-check found, what the engine is made of, and what to do next."""
from __future__ import annotations

import copy
import math
import time

import numpy as np
import torch

from . import metrics
from .bm25 import bm25_entry_scores
from .encoder import Encoder
from .evaluate import SCALE, Scorer
from .kb import KB
from .selfcheck import IN_SCOPE_SOURCES, OFF_TOPIC, OFF_TOPIC_SOURCE, SelfCheck

SYSTEMS = ("bm25", "base", "tuned")
NAMES = {"bm25": "Keyword search", "base": "Starting model", "tuned": "Your engine"}
CONFUSABLE = 0.75  # two entries whose vectors are this close are likely to be mixed up


def pick_floor(off_topic_top: np.ndarray, correct_top: np.ndarray) -> float:
    """A permissive "not in the FAQ" line: reject what scores like off-topic noise, never more than the weakest 5% of
    correct matches. Showing candidates for review costs little; hiding a real match is the worst outcome."""
    if not len(off_topic_top):  # nothing to measure refusal against: well below a typical right match, never above the weakest 5% of them
        return min(float(np.quantile(correct_top, 0.05)), 0.5 * float(np.median(correct_top))) if len(correct_top) else 0.0
    off = float(np.quantile(off_topic_top, 0.95)) + 0.02
    return min(off, float(np.quantile(correct_top, 0.05))) if len(correct_top) else off


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def faq_vectors(kb: KB, V: np.ndarray) -> tuple["KB | None", np.ndarray]:
    """The FAQ entries alone and their vectors (documents are searched, but pairs of FAQ entries are what can be confused)."""
    faq, keep = kb.faq_only()
    if faq is None or len(faq) == len(kb):
        return faq, (V if faq is not None else V[:0])
    rows = np.concatenate([np.arange(kb.view_starts[i], kb.view_starts[i] + len(kb.views_of(i))) for i in keep])
    return faq, V[rows]


def source_summary(kb: KB) -> list[dict]:
    out: dict[str, dict] = {}
    for e in kb.entries:
        d = out.setdefault(e.source or "(unnamed)", {"name": e.source or "(unnamed)", "kind": "faq" if e.kind == "faq" else "document", "faq_entries": 0, "passages": 0})
        d["faq_entries" if e.kind == "faq" else "passages"] += 1
    return list(out.values())


def faq_health(kb: KB, V: np.ndarray) -> dict:
    sums = np.add.reduceat(V, kb.view_starts, axis=0)
    C = sums / np.linalg.norm(sums, axis=1, keepdims=True).clip(min=1e-9)
    pairs = []
    if len(kb) >= 2:
        sim = C @ C.T
        iu = np.triu_indices(len(kb), k=1)
        for k in np.argsort(-sim[iu])[:6]:
            i, j = int(iu[0][k]), int(iu[1][k])
            pairs.append({"a": kb.entries[i].id, "b": kb.entries[j].id, "a_question": kb.entries[i].question, "b_question": kb.entries[j].question,
                          "similarity": float(sim[i, j]), "likely_confused": bool(sim[i, j] >= CONFUSABLE)})
    advice = []
    single = sum(len(e.questions) == 1 for e in kb.entries)
    if single:
        advice.append(f"{single} of {len(kb)} entries have only one way of asking. Training helps most with 4 or more phrasings per entry: add alternates (the alt_questions column) or generate them.")
    for p in pairs:
        if p["likely_confused"]:
            advice.append(f"\"{p['a_question']}\" and \"{p['b_question']}\" are very close (similarity {p['similarity']:.2f}); customers asking one may get the other. Merge them or add distinguishing words.")
    advice += list(kb.warnings)
    return {"entries": len(kb), "single_phrasing": single, "confusable": pairs, "advice": advice}


def entry_map(kb: KB, V: np.ndarray) -> dict | None:
    """Two-dimensional picture of the FAQ in embedding space (PCA of each entry's centre), plus what is needed to place a question on it."""
    if len(kb) < 3:
        return None
    sums = np.add.reduceat(V, kb.view_starts, axis=0)
    C = sums / np.linalg.norm(sums, axis=1, keepdims=True).clip(min=1e-9)
    mean = C.mean(0)
    _, _, vt = np.linalg.svd(C - mean, full_matrices=False)
    comps = vt[:2]
    xy = (C - mean) @ comps.T
    scale = float(np.abs(xy).max()) or 1.0
    xy = xy / scale
    return {"mean": mean.tolist(), "components": comps.tolist(), "scale": scale,
            "points": [{"id": e.id, "question": e.question, "x": float(xy[i, 0]), "y": float(xy[i, 1])} for i, e in enumerate(kb.entries)]}


def build_report(kb_final: KB, sc: SelfCheck, base: Encoder, tuned: Encoder, log: dict, device: torch.device,
                 train_seconds: float, peak_mem_mb: float, hw: dict | None = None) -> tuple[dict, dict, np.ndarray]:
    """Returns (report, thresholds, view embeddings of the final knowledge base)."""
    s_base, s_tuned = Scorer(base, sc.kb, device), Scorer(tuned, sc.kb, device)
    texts, gold = sc.test.texts, sc.test.gold
    mats = {"bm25": bm25_entry_scores(sc.kb, texts), "base": s_base.scores(texts), "tuned": s_tuned.scores(texts)}
    inscope, off = gold >= 0, gold < 0
    pq = {k: metrics.per_query(m, gold) for k, m in mats.items()}
    systems = {k: {m: float(v.mean()) for m, v in d.items()} for k, d in pq.items()}

    src = np.array(sc.test_source)[inscope]
    by_source = {}
    for s in IN_SCOPE_SOURCES:
        mask = src == s
        if mask.any():
            by_source[s] = {"n": int(mask.sum()), **{k: float(pq[k]["hit@1"][mask].mean()) for k in SYSTEMS}}

    # thresholds come from dev only; off-topic dev questions set the floor, the other off-topic questions measure it
    dev_in = sc.dev.gold >= 0
    offtopic, thresholds = {}, {}
    for name, scorer in (("base", s_base), ("tuned", s_tuned)):
        sd = scorer.scores(sc.dev.texts)
        correct = sd[dev_in].argmax(1) == sc.dev.gold[dev_in]
        floor = pick_floor(sd[~dev_in].max(1) if (~dev_in).any() else np.array([]), sd[dev_in].max(1)[correct])
        top = mats[name].max(1)
        right = inscope & (mats[name].argmax(1) == np.where(inscope, gold, -2))
        offtopic[name] = {
            "floor": floor,
            "off_topic_rejected": float((top[off] < floor).mean()) if off.any() else None,
            "real_questions_refused": float((top[inscope] < floor).mean()),
            "median_score_right": float(np.median(top[right])) if right.any() else None,
            "median_score_off_topic": float(np.median(top[off])) if off.any() else None,
        }
        thresholds[name] = floor
    final = "tuned"
    thr = {"floor": thresholds[final], "direct": None, "source": "self-check",
           "direct_reason": "needs at least 20 judged tests to prove it is safe"}

    lift = {"base->tuned": metrics.paired_bootstrap(pq["base"]["hit@1"], pq["tuned"]["hit@1"]),
            "bm25->tuned": metrics.paired_bootstrap(pq["bm25"]["hit@1"], pq["tuned"]["hit@1"])}
    loss = {"test_base": metrics.listwise_loss(mats["base"], gold, SCALE), "test_tuned": metrics.listwise_loss(mats["tuned"], gold, SCALE),
            "train_tuned": log.get("train_loss_last"), "dev_base": (log.get("dev_before") or {}).get("loss"), "dev_tuned": (log.get("dev_after") or {}).get("loss")}

    # the engine as it will be served: CPU, the whole FAQ (held-out phrasings back in), one question at a time
    cpu = torch.device("cpu")
    enc = copy.deepcopy(tuned).to(cpu)
    served = Scorer(enc, kb_final, cpu)
    sample = [t for t in texts if t][:40]
    times = []
    for t in sample:
        t0 = time.perf_counter()
        served.scores([t])
        times.append((time.perf_counter() - t0) * 1000)
    params = sum(p.numel() for p in tuned.parameters())
    V = served.V
    faq_kb, V_faq = faq_vectors(kb_final, V)
    health = faq_health(faq_kb, V_faq) if faq_kb else {"entries": 0, "single_phrasing": 0, "confusable": [], "advice": list(kb_final.warnings)}
    report = {
        "faq": {"entries": len(faq_kb) if faq_kb else 0, "passages": len(kb_final) - (len(faq_kb) if faq_kb else 0), "sources": source_summary(kb_final),
                "views": len(kb_final.views), "phrasings": sum(len(e.questions) for e in faq_kb.entries) if faq_kb else 0, "health": health},
        "selfcheck": {"counts": sc.counts(), "dev_queries": len(sc.dev), "held_out_phrasings": sc.held_out, "systems": systems, "by_source": by_source,
                      "probes_left_out": sc.dropped_probes,
                      "off_topic": offtopic, "lift": {k: list(v) for k, v in lift.items()}, "loss": loss,
                      "names": NAMES},
        "training": {k: v for k, v in log.items() if k != "curve"} | {"peak_memory_mb": peak_mem_mb, "train_seconds": train_seconds},
        "curve": log.get("curve", []),
        "model": {"base": tuned.spec.key, "hf_id": tuned.spec.hf_id, "license": tuned.spec.license, "params_millions": round(params / 1e6, 1),
                  "size_fp32_mb": round(params * 4 / 2**20, 1), "dimension": tuned.dim, "vectors": len(kb_final.views),
                  "vectors_mb": round(V.nbytes / 2**20, 3), "latency_ms_p50": float(np.percentile(times, 50)), "latency_ms_p95": float(np.percentile(times, 95)),
                  "latency_note": "one question at a time on this machine's CPU with PyTorch (the exported ONNX engine is faster)", "trained_on": str(device),
                  "tuned": bool(log.get("beat_untuned_on_dev"))},
        "map": entry_map(faq_kb, V_faq) if faq_kb else None,
        "thresholds": thr,
        "hardware": hw or {},
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    report["verdict"] = verdict(report)
    return report, thr, V


def verdict(r: dict) -> list[str]:
    s, t, h = r["selfcheck"], r["training"], r["faq"]["health"]
    sys_ = s["systems"]
    out = []
    if t.get("skipped"):
        out.append(f"Training was skipped: {t['skipped']} Your engine uses the starting model with your FAQ's embeddings.")
    elif t.get("beat_untuned_on_dev"):
        diff, lo, hi = s["lift"]["base->tuned"]
        if lo > 0:
            out.append(f"Training helped on the self-check: the right entry came first {_pct(sys_['tuned']['hit@1'])} of the time, against {_pct(sys_['base']['hit@1'])} for the starting model "
                       f"and {_pct(sys_['bm25']['hit@1'])} for keyword search.")
        else:
            out.append(f"Training won on the dev questions and is not worse on the check questions ({_pct(sys_['tuned']['hit@1'])} right first against {_pct(sys_['base']['hit@1'])} for the starting model, "
                       f"keyword search {_pct(sys_['bm25']['hit@1'])}), but that gain is within chance on this few questions, so it is not claimed as an improvement.")
    elif t.get("worse_on_check"):
        tuned_hit, base_hit = t["worse_on_check"]
        out.append(f"Training won on the dev questions but was worse on the check questions ({_pct(tuned_hit)} right first against {_pct(base_hit)}), so your engine uses the starting model "
                   "with your FAQ's embeddings. A few dev questions cannot always tell a better model from a lucky one.")
        out.append(f"On the self-check the right entry came first {_pct(sys_['base']['hit@1'])} of the time (keyword search: {_pct(sys_['bm25']['hit@1'])}).")
    else:
        out.append("Training did not beat the starting model on the self-check, so your engine uses the starting model with your FAQ's embeddings. "
                   "That is common when each question has only one way of asking: there is little to learn from.")
        out.append(f"On the self-check the right entry came first {_pct(sys_['base']['hit@1'])} of the time (keyword search: {_pct(sys_['bm25']['hit@1'])}).")
    if sum(v["n"] for v in s["by_source"].values()) >= 10 and sys_["bm25"]["hit@1"] >= sys_["tuned"]["hit@1"] + 0.07:
        out.append(f"Keyword search found the right entry first more often than the model did ({_pct(sys_['bm25']['hit@1'])} against {_pct(sys_['tuned']['hit@1'])}). That usually means the model does not "
                   "understand some of your text (another language or alphabet, product codes, names, abbreviations): check the language on the Files tab. Searching with keywords and the model together is not built yet.")
    weakest = min(s["by_source"].items(), key=lambda kv: kv[1]["tuned"], default=None)
    if weakest and weakest[1]["tuned"] < 0.8:
        out.append(f"Weakest check: \"{weakest[0]}\" ({_pct(weakest[1]['tuned'])} right first).")
    off = s["off_topic"]["tuned"]
    if off["off_topic_rejected"] is not None:
        out.append(f"Off-topic questions: {_pct(off['off_topic_rejected'])} correctly told \"not in the FAQ\"; {_pct(off['real_questions_refused'])} of the generated in-FAQ questions were wrongly refused.")
    left_out = s.get("probes_left_out") or []
    if left_out:
        out.append(f"{len(left_out)} of the {len(OFF_TOPIC)} generic off-topic questions were not used, because your FAQ talks about the same things (for example \"{left_out[0]}\"). "
                   "The refusal check is less informative; add your own out-of-scope questions as held-out questions.")
    if h["single_phrasing"]:
        out.append(f"{h['single_phrasing']} of {h['entries']} entries have a single phrasing. More phrasings per entry is the biggest lever on quality.")
    out.append("These checks are generated from your FAQ file, not from real customers. They can reveal problems but cannot prove accuracy. "
               "The real test is the Test page: try questions your customers would ask and approve or reject what comes back.")
    return out


def render_markdown(r: dict) -> str:
    if r.get("mode") == "documents":
        f, m, t = r["faq"], r["model"], r["thresholds"]
        return "\n".join(["# Engine report (documents only)", ""] + [f"- {v}" for v in r["verdict"]] + ["", "## Sources", ""] +
                         [f"- {x['name']}: {x['passages']} passages" for x in f["sources"]] +
                         ["", "## Engine", "", f"- Base model `{m['hf_id']}` (licence {m['license']}), {m['params_millions']}M parameters; {m['vectors']} vectors",
                          f"- \"Not in the knowledge base\" below a score of {t['floor']:.2f} ({t['source']})"]) + "\n"
    if r.get("mode") == "heldout":
        from .heldout_report import render_heldout_markdown

        return render_heldout_markdown(r)
    s, m, t = r["selfcheck"], r["model"], r["training"]
    L = ["# Engine report", ""] + [f"- {v}" for v in r["verdict"]] + ["", "## Self-check (generated from your FAQ, not real customers)", "",
         "| System | Right first | P@3 | R@3 | P@5 | R@5 | MRR@10 | nDCG@10 |", "|---|---|---|---|---|---|---|---|"]
    for k in SYSTEMS:
        x = s["systems"][k]
        L.append(f"| {NAMES[k]} | {_pct(x['hit@1'])} | {_pct(x['p@3'])} | {_pct(x['hit@3'])} | {_pct(x['p@5'])} | {_pct(x['hit@5'])} | {x['mrr@10']:.3f} | {x['ndcg@10']:.3f} |")
    L += ["", "Right first = Hit@1 = P@1 = R@1. With one right entry per question, R@K = Hit@K and P@K = Hit@K / K.", "", "| Kind of question | Questions | " + " | ".join(NAMES[k] for k in SYSTEMS) + " |",
          "|---|---|---|---|---|"]
    for name, x in s["by_source"].items():
        L.append(f"| {name} | {x['n']} | " + " | ".join(_pct(x[k]) for k in SYSTEMS) + " |")
    lo = s["loss"]
    L += ["", f"Loss (lower is better): train {lo['train_tuned'] if lo['train_tuned'] is not None else float('nan'):.3f}; dev {lo['dev_tuned'] if lo['dev_tuned'] is not None else float('nan'):.3f}; "
          f"self-check test {lo['test_tuned']:.3f} (starting model: {lo['test_base']:.3f}).", "",
          "## Engine", "", f"- Base model `{m['hf_id']}` (licence {m['license']}), {m['params_millions']}M parameters, {m['size_fp32_mb']} MB; {m['vectors']} vectors of {m['dimension']} numbers",
          f"- Latency {m['latency_ms_p50']:.1f} ms median, {m['latency_ms_p95']:.1f} ms p95 ({m['latency_note']})",
          f"- Training: {t.get('steps', 0)} steps in {t.get('train_seconds', 0):.0f} s on {m['trained_on']}, peak memory {t.get('peak_memory_mb', 0):.0f} MB",
          f"- \"Not in the FAQ\" below a score of {r['thresholds']['floor']:.2f}; answering without an LLM: {'not yet proven' if r['thresholds']['direct'] is None else 'from ' + format(r['thresholds']['direct'], '.2f')}", ""]
    if r["faq"]["health"]["advice"]:
        L += ["## FAQ health", ""] + [f"- {a}" for a in r["faq"]["health"]["advice"]]
    return "\n".join(L) + "\n"


def build_documents_report(kb: KB, base: Encoder, device: torch.device, hw: dict | None, floor: float | None = None) -> tuple[dict, dict, np.ndarray]:
    """Only documents (no FAQ pairs): nothing to train on and no questions to check against, so the report says exactly that."""
    from .selfcheck import usable_probes

    OFF_TOPIC = usable_probes(kb.entries)[0]  # the generic questions this knowledge base does not itself cover
    cpu = torch.device("cpu")
    scorer = Scorer(copy.deepcopy(base).to(cpu), kb, cpu)
    off_top = scorer.scores(OFF_TOPIC).max(1) if OFF_TOPIC else np.array([])
    line = floor if floor is not None else pick_floor(off_top, np.array([]))
    thr = {"floor": line, "direct": None, "source": "manual" if floor is not None else "unrelated questions (no FAQ questions to check against)",
           "direct_reason": "a document passage is never returned as a stored answer; answers built from documents are assembled and referenced on the Test page"}
    params = sum(p.numel() for p in base.parameters())
    report = {
        "mode": "documents",
        "faq": {"entries": 0, "passages": len(kb), "sources": source_summary(kb), "views": len(kb.views), "phrasings": 0,
                "health": {"entries": 0, "single_phrasing": 0, "confusable": [], "advice": list(kb.warnings)}},
        "training": {"skipped": "there are no FAQ question and answer pairs to train on", "steps": 0, "beat_untuned_on_dev": False, "train_seconds": 0.0, "peak_memory_mb": 0.0},
        "curve": [],
        "model": {"base": base.spec.key, "hf_id": base.spec.hf_id, "license": base.spec.license, "params_millions": round(params / 1e6, 1),
                  "size_fp32_mb": round(params * 4 / 2**20, 1), "dimension": base.dim, "vectors": len(kb.views), "vectors_mb": round(scorer.V.nbytes / 2**20, 3),
                  "latency_ms_p50": 0.0, "latency_ms_p95": 0.0, "latency_note": "", "trained_on": str(device), "tuned": False},
        "map": None, "thresholds": thr, "hardware": hw or {}, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": [f"Only documents were added ({len(kb)} passages): they are searched with the unchanged base model. There are no FAQ pairs to train on and no questions to check against.",
                    "Add a FAQ file to train, or a held-out question file to measure how well the right passage is found. Until then the Test page is the way to judge it.",
                    "Documents are embedded and searched like a FAQ, and answers are built from their own sentences with a reference (Test page); they are never returned as a stored FAQ answer."],
    }
    return report, thr, scorer.V
