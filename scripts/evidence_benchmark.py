"""Measures evidence-aware answering on a manually verified set (examples/evidence/{dev,regression,locked}.csv) with the same retrieval for every implementation.

    python scripts/evidence_benchmark.py --set dev --impl before        # the implementation frozen in scripts/legacy_evidence_v1.py
    python scripts/evidence_benchmark.py --set dev --impl after         # the current runtime/evidence.py
    python scripts/evidence_benchmark.py --set locked --impl after      # the final benchmark: aggregate numbers only, run it once, never tune on it

A response is judged CORRECT when it is of the expected kind (answered / partial / declined / conflict), and for answers: every `must_contain` fact is in the answer, no
`must_not_contain` text is, and every `cite` entry is among the cited sources. Metrics (definitions in the output): answer coverage, correct answer rate, false-answer rate,
abstention precision and recall, evidence-sufficiency precision and recall, multi-part completion, partial handling, conflict detection.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
EVID = ROOT / "examples" / "evidence"


def load_module(impl: str):
    if impl == "before":
        spec = importlib.util.spec_from_file_location("evidence_v1", ROOT / "scripts" / "legacy_evidence_v1.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["evidence_v1"] = mod
        spec.loader.exec_module(mod)
        return mod
    from faqengine.runtime import evidence

    return evidence


def build_run(out: Path):
    from faqengine.auto import run_auto

    if not (out / "report.json").exists():
        run_auto([EVID / "pawfect_faq.csv", EVID / "pawfect_handbook.md", EVID / "policy_update.md"], out, device="auto")
    return out


def normalise(r: dict) -> dict:
    """The old implementation reported a partly answered question as `insufficient` with a list of what it knew; call that what it is."""
    if r["outcome"] == "insufficient" and r.get("partial"):
        r = {**r, "outcome": "partial", "partial_answer": " ".join(x["text"] for x in r["partial"]), "unanswered_parts": r.get("unanswered_parts", [])}
    return r


def judge(row: dict, r: dict) -> tuple[bool, bool]:
    """(response is of the expected kind and factually right, an answer was given)."""
    r = normalise(r)
    want, outcome = row["expected_outcome"], r["outcome"]
    answered = outcome in ("faq", "grounded")
    text = (r.get("answer") or r.get("partial_answer") or "").lower()
    contain = [x.lower() for x in row["must_contain"].split(";") if x]
    forbid = [x.lower() for x in row["must_not_contain"].split(";") if x]
    cited = {s["id"] for s in r.get("sources", [])}
    cites = [c for c in row["cite"].split(";") if c]
    facts = all(c in text for c in contain) and not any(f in text for f in forbid) and all(c in cited for c in cites)
    if want in ("faq", "grounded"):
        return answered and facts, answered
    if want == "partial":
        return outcome == "partial" and facts and bool(r.get("unanswered_parts")), False
    if want == "conflict":
        return outcome == "conflict", False
    return outcome in ("insufficient", "clarification"), answered  # asking the customer to say more is also not giving an answer


def main() -> None:
    from faqengine.studio.engine import ProjectEngine
    from faqengine.studio.store import read_json

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default="dev", choices=["dev", "regression", "locked"])
    ap.add_argument("--impl", default="after", choices=["before", "after"])
    ap.add_argument("--run", default=str(ROOT / "runs" / "evidence-kb"))
    ap.add_argument("--detail", action="store_true", help="print every question (refused for the locked set)")
    ap.add_argument("--json", help="write the numbers here")
    args = ap.parse_args()
    if args.set == "locked" and args.detail:
        sys.exit("the locked benchmark reports aggregate numbers only: looking at its questions one by one is how a benchmark stops being independent")
    rows = [{k: (v or "") for k, v in r.items()} for r in csv.DictReader(open(EVID / f"{args.set}.csv", encoding="utf-8"))]
    run = build_run(Path(args.run))
    eng = ProjectEngine(run)
    th = read_json(run / "thresholds.json")
    ev = load_module(args.impl)
    sim = eng.sentence_similarity()
    tally = {"n": len(rows), "answerable": 0, "answered": 0, "answered_ok": 0, "false_answers": 0, "should_decline": 0, "declined": 0, "declined_ok": 0, "sufficient_true": 0,
             "sufficient_pred": 0, "sufficient_both": 0, "multi": 0, "multi_ok": 0, "partial": 0, "partial_ok": 0, "conflict": 0, "conflict_ok": 0}
    by_cat: dict[str, list[int]] = {}
    for row in rows:
        if args.impl == "after":  # the whole pipeline as the website runs it: hybrid retrieval, evidence, response (no language model)
            r = eng.respond(row["question"], th)
        else:
            faq, passages = eng.candidates(row["question"], 8, "semantic")
            r = ev.respond(row["question"], faq, passages, sim=sim, sem_min=th.get("evidence_sim_min", ev.SEM_MIN))
        ok, answered = judge(row, r)
        want = row["expected_outcome"]
        by_cat.setdefault(row["category"], [0, 0])
        by_cat[row["category"]][0] += 1
        by_cat[row["category"]][1] += ok
        if want in ("faq", "grounded"):
            tally["answerable"] += 1
            tally["sufficient_true"] += 1
            tally["answered"] += answered
            tally["answered_ok"] += ok
            if row["category"] == "multi-part":
                tally["multi"] += 1
                tally["multi_ok"] += ok
        if answered:
            tally["false_answers"] += not ok
        if want == "insufficient":
            tally["should_decline"] += 1
            tally["declined_ok"] += ok
        if normalise(r)["outcome"] in ("insufficient", "clarification"):
            tally["declined"] += 1
        if r["outcome"] in ("faq", "grounded"):
            tally["sufficient_pred"] += 1
            tally["sufficient_both"] += want in ("faq", "grounded")
        if want == "partial":
            tally["partial"] += 1
            tally["partial_ok"] += ok
        if want == "conflict":
            tally["conflict"] += 1
            tally["conflict_ok"] += ok
        if args.detail:
            print(f"{'ok ' if ok else 'BAD'} [{row['category']:11s}] {row['question'][:62]:62s} -> {normalise(r)['outcome']:12s} {(r.get('answer') or r.get('partial_answer') or r.get('message') or '')[:70]!r}")
    pct = lambda a, b: None if not b else round(a / b, 3)  # noqa: E731
    m = {"questions": tally["n"], "answer_coverage": pct(tally["answered"], tally["answerable"]), "correct_answer_rate": pct(tally["answered_ok"], tally["answerable"]),
         "false_answer_rate": pct(tally["false_answers"], tally["sufficient_pred"]), "abstention_precision": pct(tally["declined_ok"], tally["declined"]),
         "abstention_recall": pct(tally["declined_ok"], tally["should_decline"]), "sufficiency_precision": pct(tally["sufficient_both"], tally["sufficient_pred"]),
         "sufficiency_recall": pct(tally["sufficient_both"], tally["sufficient_true"]), "multi_part_completion": pct(tally["multi_ok"], tally["multi"]),
         "partial_handling": pct(tally["partial_ok"], tally["partial"]), "conflict_detection": pct(tally["conflict_ok"], tally["conflict"]),
         "by_category_correct": {k: f"{v[1]}/{v[0]}" for k, v in by_cat.items()}, "counts": tally}
    print(json.dumps(m, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(m, indent=1))


if __name__ == "__main__":
    main()
