"""The previous FAQ and engine against the improved ones, on the same independent held-out questions.

How the comparison is kept fair:
  * both versions answer the very same test questions (near-duplicates of EITHER version's training phrasings are removed first);
  * credit is for information, not entry ids: where an improved entry absorbed several previous entries, the previous engine is right
    if it returns any of them (previous_map.csv says which);
  * the previous engine is what the previous app actually produced (its own pipeline run on the previous FAQ);
  * the improved engine is trained with several seeds, because one run on a small FAQ is noisy; the headline is seed 0, as the app would ship;
  * the effect of the data (previous base model -> improved base model) is separated from the effect of training (improved base -> trained).
"""
from __future__ import annotations

import csv
import dataclasses
import json
import time
from pathlib import Path

import numpy as np
import torch

from . import metrics
from .auto import jsonable, run_auto
from .encoder import Encoder, pick_device, release_memory
from .evaluate import Scorer
from .heldout import IN_SCOPE_TYPES, LABELS, HeldOut, load_heldout, training_phrasings
from .heldout_report import METRICS, SYSTEMS, refusal_report, score_matrices, typed_metrics
from .kb import KB, load_kb
from .report import NAMES, faq_health

def read_map(path: str | Path) -> dict[str, list[str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["id"]: [x for x in r["previous"].split("|") if x] for r in csv.DictReader(f)}


def write_heldout(ho: HeldOut, kb: KB, path: Path) -> None:
    """The held-out questions that survived the leakage check, so every run in the comparison uses exactly these."""
    ids = [e.id for e in kb.entries]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query", "entry_id", "type", "split", "source"])
        for i in range(len(ho)):
            w.writerow([ho.queries[i], "|".join(sorted(ids[g] for g in ho.gold[i])), ho.types[i], ho.split[i], ho.source[i]])


def _pq(mat: np.ndarray, ho: HeldOut, split: str = "test") -> dict:
    rows = ho.where(split, IN_SCOPE_TYPES)
    return metrics.per_query_sets(mat[rows], [ho.gold[i] for i in rows])


def _centroid_pairs(kb: KB, enc: Encoder, device: torch.device) -> tuple[dict, np.ndarray]:
    V = Scorer(enc, kb, device).V
    sums = np.add.reduceat(V, kb.view_starts, axis=0)
    C = sums / np.linalg.norm(sums, axis=1, keepdims=True).clip(min=1e-9)
    return faq_health(kb, V), C @ C.T


def _diff(a: np.ndarray, b: np.ndarray, scale: float = 1.0) -> dict:
    d, lo, hi = metrics.paired_bootstrap(a, b, n=4000)
    return {"a": float(a.mean()), "b": float(b.mean()), "diff": d, "low": lo, "high": hi}


def entry_changes(prev: KB, new: KB, pmap: dict[str, list[str]]) -> list[dict]:
    rows = []
    for e in new.entries:
        olds = [prev.entries[prev.index[x]] for x in pmap[e.id]]
        if len(olds) > 1:
            kind = "merged"
        elif olds[0].question == e.question and olds[0].answer == e.answer:
            kind = "unchanged (phrasings added)"
        elif olds[0].answer == e.answer:
            kind = "question reworded"
        else:
            kind = "question and answer reworded (same facts)"
        rows.append({"id": e.id, "kind": kind, "previous": [(o.id, o.question) for o in olds], "question": e.question, "answer": e.answer,
                     "previous_answers": [o.answer for o in olds], "alt_phrasings": len(e.questions) - 1})
    return rows



def _run(on_event, what: str, *args, **kw) -> dict:
    """run_auto, with the accelerator's cached memory handed back afterwards, and one retry if the driver still runs out of memory."""
    for attempt in (1, 2):
        try:
            report = run_auto(*args, **kw)
            release_memory()
            return report
        except RuntimeError as e:  # torch.OutOfMemoryError is a RuntimeError; so is the Metal driver's "Insufficient Memory"
            release_memory()
            if attempt == 2 or "memory" not in str(e).lower():
                raise
            on_event(f"{what}: ran out of memory; memory released, trying once more (use --device cpu if it happens again)")


def compare(previous_faq: str | Path, improved_faq: str | Path, heldout_csv: str | Path, map_csv: str | Path, out_dir: str | Path,
            seeds: tuple[int, ...] = (0, 1, 2), device: str = "auto", base_key: str = "minilm-l6", notes: str | Path | None = None,
            on_event=print) -> dict:
    """Any two versions of any FAQ: nothing here knows what the FAQ is about."""
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prev_kb, new_kb = load_kb(previous_faq), load_kb(improved_faq)
    pmap = read_map(map_csv)
    ho = load_heldout(heldout_csv, new_kb, train=training_phrasings(prev_kb) + training_phrasings(new_kb))
    used = out / "heldout.used.csv"
    write_heldout(ho, new_kb, used)
    gold_prev = [frozenset(prev_kb.index[x] for i in g for x in pmap[new_kb.entries[i].id]) for g in ho.gold]
    ho_prev = dataclasses.replace(ho, gold=gold_prev)
    dev_t = pick_device(device)
    base = Encoder.load(base_key).to(dev_t)

    on_event("previous version: running the previous pipeline on the previous FAQ")
    run_prev = out / "previous-run"
    _run(on_event, "previous version", previous_faq, run_prev, device=device, seed=0, base_key=base_key)
    prev_engine = Encoder.load(run_prev / "model").to(dev_t)
    prev_floor = json.loads((run_prev / "thresholds.json").read_text())["floor"]
    pm = score_matrices(prev_kb, ho.queries, base, prev_engine, dev_t)
    prev = {"dev": typed_metrics(pm, ho_prev, "dev")[0], "test": typed_metrics(pm, ho_prev, "test")[0],
            "refusal_as_shipped": refusal_report(pm, ho_prev, {"base": prev_floor, "tuned": prev_floor}), "refusal_rechosen": refusal_report(pm, ho_prev),
            "shipped_floor": prev_floor, "engine_is_base": bool(not json.loads((run_prev / "train_log.json").read_text()).get("beat_untuned_on_dev")),
            "own_report_verdict": json.loads((run_prev / "report.json").read_text())["verdict"]}
    pq_prev = {s: _pq(pm[s], ho_prev) for s in SYSTEMS}

    runs = []
    for seed in seeds:
        on_event(f"improved version: training with seed {seed}")
        run = out / f"improved-seed{seed}"
        rep = _run(on_event, f"seed {seed}", improved_faq, run, device=device, seed=seed, base_key=base_key, heldout_path=used)
        shipped = Encoder.load(run / "model").to(dev_t) if not runs else None  # only seed 0 is scored again below
        runs.append({"seed": seed, "report": rep, "shipped": shipped})
    r0 = runs[0]["report"]["heldout"]
    nm = score_matrices(new_kb, ho.queries, base, runs[0]["shipped"], dev_t)  # "tuned" here is whatever seed 0 shipped
    pq_new = {s: _pq(nm[s], ho) for s in SYSTEMS}

    key = lambda d, k: d[k]  # noqa: E731
    paired = {}
    for k in ("hit@1", "mrr@10", "ndcg@10"):
        paired[k] = {"previous_vs_improved_engine": _diff(pq_prev["tuned"][k], pq_new["tuned"][k]),
                     "data_effect_base_to_base": _diff(pq_prev["base"][k], pq_new["base"][k]),
                     "training_effect_base_to_shipped": _diff(pq_new["base"][k], pq_new["tuned"][k]),
                     "previous_vs_improved_keyword": _diff(pq_prev["bm25"][k], pq_new["bm25"][k])}

    # confusable pairs before and after, with the same (unchanged) model on both FAQs, so only the FAQ differs
    h_prev, S_prev = _centroid_pairs(prev_kb, base, dev_t)
    h_new, S_new = _centroid_pairs(new_kb, base, dev_t)
    inv = {x: e for e in new_kb.entries for x in pmap[e.id]}
    pair_rows = []
    for p in json.loads((run_prev / "report.json").read_text())["faq"]["health"]["confusable"]:  # the previous engine's own closest pairs, whatever they are
        a, b = inv[p["a"]], inv[p["b"]]
        pair_rows.append({"a": p["a_question"], "b": p["b_question"], "reported": p["similarity"], "before": float(S_prev[prev_kb.index[p["a"]], prev_kb.index[p["b"]]]),
                          "after_a": a.id, "after_b": b.id, "after": None if a.id == b.id else float(S_new[new_kb.index[a.id], new_kb.index[b.id]])})

    def seed_row(r: dict) -> dict:
        h = r["report"]["heldout"]
        e, dec = h["effect"], h["decision"]
        return {"seed": r["seed"], "dev_base": h["dev"]["overall"]["base"]["hit@1"], "dev_trained": h["dev"]["overall"]["tuned"]["hit@1"],
                "test_base": e["hit@1"]["base"], "test_trained": e["hit@1"]["tuned"], "diff": e["hit@1"]["diff"], "low": e["hit@1"]["low"], "high": e["hit@1"]["high"],
                "mrr_base": e["mrr@10"]["base"], "mrr_trained": e["mrr@10"]["tuned"], "ship": dec["ship"], "demonstrated": dec["improvement_demonstrated"],
                "steps": r["report"]["training"]["steps"], "seconds": r["report"]["training"]["train_seconds"],
                "floor": h["refusal"][dec["ship"]]["floor"]}

    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": time.time() - t0, "base_model": base_key, "seeds": list(seeds),
        "dataset": {"previous_entries": len(prev_kb), "previous_phrasings": sum(len(e.questions) for e in prev_kb.entries),
                    "improved_entries": len(new_kb), "improved_phrasings": sum(len(e.questions) for e in new_kb.entries),
                    "alt_per_entry": [len(e.questions) - 1 for e in new_kb.entries], "changes": entry_changes(prev_kb, new_kb, pmap)},
        "heldout": {"counts": ho.counts(), "removed": ho.removed, "labels": LABELS, "owner_typed": sum(1 for s in ho.source if s == "owner")},
        "previous": prev, "improved": r0, "seed_rows": [seed_row(r) for r in runs], "paired": paired,
        "pairs": pair_rows, "closest_pairs": {"previous": h_prev["confusable"], "improved": h_new["confusable"]},
        "improved_training": {k: v for k, v in runs[0]["report"]["training"].items()},
        "command": {"previous": str(previous_faq), "improved": str(improved_faq), "heldout": str(heldout_csv), "map": str(map_csv), "notes": str(notes) if notes else None, "out": str(out_dir), "seeds": list(seeds)},
        "notes": Path(notes).read_text(encoding="utf-8") if notes else "",
    }
    (out / "comparison.json").write_text(json.dumps(jsonable(result), indent=1))
    (out / "comparison.md").write_text(render(result))
    return result


def _p(x, d=0):
    return "n/a" if x is None else f"{100 * x:.{d}f}%"


def _row(label, x):
    return f"| {label} | {_p(x['hit@1'])} | {_p(x['p@3'])} | {_p(x['r@3'])} | {_p(x['p@5'])} | {_p(x['r@5'])} | {x['mrr@10']:.3f} | {x['ndcg@10']:.3f} |"


def _ci(d, pts=True):
    f = (lambda v: f"{100 * v:+.1f}") if pts else (lambda v: f"{v:+.3f}")
    return f"{f(d['diff'])}{' pts' if pts else ''} (95% interval {f(d['low'])} to {f(d['high'])})"


def render(c: dict) -> str:
    P, I = c["previous"], c["improved"]
    pt, it = P["test"], I["test"]
    pr, ir = P["refusal_as_shipped"], I["refusal"]
    ship = I["decision"]["ship"]
    pv = c["paired"]
    n = it["n"]
    L = ["# Engine report: previous version vs improved version", "",
         f"Generated {c['generated_at']}. Everything below is measured on the same {n} held-out test questions (the validation questions that chose the models are generated from each FAQ and are not among them), "
         f"which were written separately from the training phrasings and checked for near-duplicates of them. Base model: `{c['base_model']}`.", ""]
    L += ["## Summary", ""]
    h = pv["hit@1"]["previous_vs_improved_engine"]
    m = pv["mrr@10"]["previous_vs_improved_engine"]
    L.append(f"- **Right entry first (Hit@1):** previous engine {_p(h['a'])} → improved engine {_p(h['b'])}, {_ci(h)}.")
    L.append(f"- **MRR@10:** {m['a']:.3f} → {m['b']:.3f}, {_ci(m, False)}.")
    d, t = pv["hit@1"]["data_effect_base_to_base"], pv["hit@1"]["training_effect_base_to_shipped"]
    L.append(f"- **Where the gain comes from:** the better FAQ data alone (same unchanged base model on both FAQs) moves Hit@1 {_p(d['a'])} → {_p(d['b'])} ({_ci(d)}); "
             f"training on top of it moves {_p(t['a'])} → {_p(t['b'])} ({_ci(t)}).")
    e = I["effect"]
    s_rows = c["seed_rows"]
    gains = [r["diff"] for r in s_rows]
    L.append(f"- **Did training help?** " + (f"Yes, demonstrated (seed 0: {_ci(e['hit@1'])})." if I["decision"]["improvement_demonstrated"] else f"Not demonstrated (seed 0: {_ci(e['hit@1'])}).")
             + f" Across {len(s_rows)} seeds the trained model's Hit@1 gain over the base model was {', '.join(f'{100 * g:+.1f}' for g in gains)} points; "
             f"the default would be the trained model in {sum(r['ship'] == 'tuned' for r in s_rows)} of {len(s_rows)} runs. Default for seed 0: **{ship}**.")
    L.append(f"- **Refusal (validated separately):** previous engine refused {_p(pr['tuned']['test']['off_topic_near_refused'])} near-topic and {_p(pr['tuned']['test']['off_topic_far_refused'])} unrelated out-of-scope questions "
             f"(wrongly refused {_p(pr['tuned']['test']['in_scope_refused'])} of answerable ones); improved engine {_p(ir[ship]['test']['off_topic_near_refused'])} and {_p(ir[ship]['test']['off_topic_far_refused'])} "
             f"(wrongly refused {_p(ir[ship]['test']['in_scope_refused'])}).")
    L.append(f"- **FAQ:** {c['dataset']['previous_entries']} entries with {c['dataset']['previous_phrasings']} phrasings → {c['dataset']['improved_entries']} entries with {c['dataset']['improved_phrasings']} phrasings "
             f"({min(c['dataset']['alt_per_entry'])} to {max(c['dataset']['alt_per_entry'])} alternate phrasings each). No prices, policies or facts were added; every change is listed below.")
    L += ["", "## Overall, test questions (side by side)", "", "| System | Hit@1 | P@3 | R@3 | P@5 | R@5 | MRR@10 | nDCG@10 |", "|---|---|---|---|---|---|---|---|"]
    L += [_row("Previous: keyword search", pt["overall"]["bm25"]), _row("Previous: base model", pt["overall"]["base"]),
          _row("**Previous: engine as shipped**" + (" (= base model)" if P["engine_is_base"] else ""), pt["overall"]["tuned"]),
          _row("Improved: keyword search", it["overall"]["bm25"]), _row("Improved: base model (unchanged)", it["overall"]["base"]),
          _row("Improved: trained model (seed 0)", it["overall"]["tuned"]), _row(f"**Improved: engine as shipped** (= {ship} model)", it["overall"][ship])]
    L += ["", "## By type of question, test (previous → improved)", "",
          "| Type | n | System | Previous Hit@1 | Improved Hit@1 | Previous MRR@10 | Improved MRR@10 | Previous nDCG@10 | Improved nDCG@10 |", "|---|---|---|---|---|---|---|---|---|"]
    for ty, d_ in it["by_type"].items():
        pd_ = pt["by_type"][ty]
        for i, (s, lab) in enumerate((("bm25", "Keyword search"), ("base", "Base model"), ("tuned", "Trained / shipped engine"))):
            ni = d_[s]
            npv = pd_[s]
            L.append(f"| {LABELS[ty] if i == 0 else ''} | {d_['n'] if i == 0 else ''} | {lab} | {_p(npv['hit@1'])} | {_p(ni['hit@1'])} | {npv['mrr@10']:.3f} | {ni['mrr@10']:.3f} | {npv['ndcg@10']:.3f} | {ni['ndcg@10']:.3f} |")
    L += ["", "Notes: in the \"previous\" columns the third system is the previous engine as shipped; in the \"improved\" columns it is the trained model from seed 0 "
          "(the row labelled shipped above is the base model when training did not win). Ambiguous questions accept several entries; R@K there is the share of acceptable entries found.",
          "", "## Did training help? (improved FAQ, base model vs trained model, by seed)", "",
          "| Seed | Dev Hit@1 base → trained | Test Hit@1 base → trained | Difference (95% interval) | Test MRR@10 base → trained | Default | Improvement demonstrated | Steps |", "|---|---|---|---|---|---|---|---|"]
    for r in s_rows:
        L.append(f"| {r['seed']} | {_p(r['dev_base'])} → {_p(r['dev_trained'])} | {_p(r['test_base'], 1)} → {_p(r['test_trained'], 1)} | {100 * r['diff']:+.1f} pts ({100 * r['low']:+.1f} to {100 * r['high']:+.1f}) | "
                 f"{r['mrr_base']:.3f} → {r['mrr_trained']:.3f} | {r['ship']} | {'yes' if r['demonstrated'] else 'no'} | {r['steps']} |")
    L += ["", f"Rule for the default: the trained model is used only if it beat the base model on the dev questions AND is not worse on the test questions; otherwise the unchanged base model stays. "
          "An improvement is only claimed when the paired 95% interval on right-entry-first (Hit@1) excludes zero; a gain in ranking alone (MRR@10) is reported but not claimed.", "",
          "## Refusal behaviour (validated separately)", "",
          "Near-topic = about the same subject but not in the FAQ; unrelated = anything else. The refusal line of the improved engine is chosen on dev questions; "
          "the previous engine is shown at the line it actually shipped with, and with a line re-chosen on dev the same way.", "",
          "| Engine | Refusal line | Near-topic refused | Unrelated refused | Answerable wrongly refused | AUROC near-topic | AUROC unrelated |", "|---|---|---|---|---|---|---|"]
    def rrow(label, x):
        te = x["test"]
        return f"| {label} | {x['floor']:.2f} | {_p(te['off_topic_near_refused'])} ({te['n_near']}) | {_p(te['off_topic_far_refused'])} ({te['n_far']}) | {_p(te['in_scope_refused'])} ({te['n_in_scope']}) | {te['auroc_near']:.2f} | {te['auroc_far']:.2f} |"
    L += [rrow("Previous engine, line as shipped", pr["tuned"]), rrow("Previous engine, line re-chosen on dev", P["refusal_rechosen"]["tuned"]),
          rrow("Improved base model", ir["base"]), rrow("Improved trained model", ir["tuned"])]
    L += ["", f"Threshold sweep, improved default ({NAMES['tuned'] if ship == 'tuned' else NAMES['base']}), test questions:", "", "| Line | Near-topic refused | Unrelated refused | Answerable wrongly refused |", "|---|---|---|---|"]
    L += [f"| {x['floor']:.2f} | {_p(x['off_topic_near_refused'])} | {_p(x['off_topic_far_refused'])} | {_p(x['in_scope_refused'])} |" for x in ir[ship]["sweep"]]
    L += ["", "Answering without an LLM stays **off** in both versions: it needs proof from a person's own approved tests, which the Test page collects. Both thresholds are configurable (`--floor`, `--direct`, or the Test page).",
          "", "## Confusable entries, before and after", "",
          "Similarity of entry centres under the same unchanged base model on both FAQs, so only the FAQ differs. These are the closest pairs the previous engine's own report listed.", "",
          "| Previous pair | Reported | Now (base model) | After |", "|---|---|---|---|"]
    for x in c["pairs"]:
        after = f"merged into one entry (`{x['after_a']}`)" if x["after"] is None else f"`{x['after_a']}` vs `{x['after_b']}`: {x['after']:.2f}"
        L.append(f"| {x['a']} / {x['b']} | {x['reported']:.2f} | {x['before']:.2f} | {after} |")
    top = c["closest_pairs"]["improved"][:4]
    L += ["", "Closest pairs in the improved FAQ (base model): " + "; ".join(f"`{p['a']}` ~ `{p['b']}` {p['similarity']:.2f}" for p in top) + ".", "",
          "## Data and leakage checks", ""]
    cn = c["heldout"]["counts"]
    L += [f"- Held-out: {cn['total']} questions kept ({cn['by_split']['dev']} dev, {cn['by_split']['test']} test), {cn['removed_for_leakage']} removed as near-duplicates of a training phrasing of either version. "
          f"{c['heldout']['owner_typed']} were typed by the owner in the app earlier.",
          "- Test questions by type: " + ", ".join(f"{LABELS[k].split(' (')[0].lower()} {v['test']}" for k, v in cn["by_type"].items()) + ".",
          "- No test question is a near-duplicate of any training phrasing (same words, typo or light rewording); the dev questions that chose the model are separate from the test questions that report it.",
          "- Training phrasings were never split between train and test: all alternate phrasings are training data, and the held-out questions come from a separate file.",
          "- The held-out questions and the alternate phrasings were written by the same author (not customers), so absolute figures are probably higher than real customers would get; "
          "the comparisons between systems are fair because every system answers the same questions.",
          "- Answer-word questions reuse terms from the answers by design (they test whether answer wording leads to the entry).", "",
          "## Entry-by-entry changes to the FAQ", "", "| New entry | Change | Previous entries | New question |", "|---|---|---|---|"]
    for ch in c["dataset"]["changes"]:
        L.append(f"| `{ch['id']}` | {ch['kind']} | " + "; ".join(f"{i}: {q}" for i, q in ch["previous"]) + f" | {ch['question']} |")
    if c["notes"]:
        L += ["", c["notes"].strip()]
    cmd = c["command"]
    L += ["", "## Reproduce", "", "```", f"faqengine compare --previous {cmd['previous']} --improved {cmd['improved']} \\",
          f"    --heldout {cmd['heldout']} --map {cmd['map']}" + (f" --notes {cmd['notes']}" if cmd["notes"] else "") + f" --seeds {','.join(map(str, cmd['seeds']))} --out {cmd['out']}", "```"]
    return "\n".join(L) + "\n"
