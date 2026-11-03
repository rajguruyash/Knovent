"""Runs the model-independent reasoning pipeline (runtime/pipeline.py) in DETERMINISTIC mode over the example question sets and reports, separately: end-to-end correctness, sufficiency precision
and recall by status, the share of claims verification had to remove, citation correctness, conflict handling, partial-answer correctness, unnecessary abstention, structured-reasoning
correctness, and the time of each stage. It also compares the pipeline's outcome with the engine's own (answer.respond): in deterministic mode they must agree.

    python scripts/pipeline_report.py [--sets reasoning:dev reasoning:regression unified:dev ...] [--json runs/pipeline/report.json]

The locked and verify sets are refused. These are DIAGNOSTICS of a deterministic pipeline on sets it was developed with: they are not independent benchmark results, and no mock provider or mock
composer is run here (nothing in this report says anything about a language model). Query-understanding and evidence-validation accuracy are measured on labelled cases (tests/pipeline_cases.py).
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import unified_benchmark as UB  # noqa: E402
from faqengine.runtime import answer, evidence, evidence_check, pipeline, understand  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402
from pipeline_cases import EVIDENCE_CASES, KNOWN, QUERY_CASES, Sim  # noqa: E402

STATUS = {"answered": {"SUFFICIENT"}, "partial": {"PARTIAL"}, "conflict": {"CONFLICTED"}, "insufficient": {"INSUFFICIENT", "AMBIGUOUS"}, "clarification": {"INSUFFICIENT", "AMBIGUOUS"}}
STRUCTURED = {"structured", "comparison", "price_calc", "limit_check", "feature_compare", "semantic_header", "missing_value"}


def labelled_cases() -> dict:
    ok = 0
    for q, want in QUERY_CASES:
        p = understand.understand(q, KNOWN, today=date(2026, 6, 1))
        good = True
        good &= "intent" not in want or p.intent == want["intent"]
        good &= "conditions" not in want or want["conditions"] <= {c.text.lower() for c in p.conditions}
        good &= "qualifiers" not in want or want["qualifiers"] <= set(p.qualifiers)
        good &= "claim_types" not in want or {c.claim_type for c in p.conditions if c.claim_type} == want["claim_types"]
        good &= "qualifier_on_condition" not in want or any(c.qualifier == want["qualifier_on_condition"] for c in p.conditions)
        good &= "comparison" not in want or (p.comparison or {}).get("operation") == want["comparison"]
        good &= "needs_structured" not in want or p.needs_structured is want["needs_structured"]
        good &= "entities" not in want or want["entities"] <= {e.lower() for e in p.entities}
        good &= "day" not in want or p.temporal["day"] == want["day"]
        good &= "parts" not in want or len(p.parts) == want["parts"]
        ok += good
    ev_ok = 0
    for _, q, h, status in EVIDENCE_CASES:
        plan = understand.understand(q, KNOWN)
        e = evidence_check.normalize([h], "t1")
        ev_ok += evidence_check.validate(plan, e, "t1", date(2026, 6, 1), Sim())[0].status == status
    return {"query_understanding_accuracy": f"{ok}/{len(QUERY_CASES)}", "evidence_validation_accuracy": f"{ev_ok}/{len(EVIDENCE_CASES)}"}


def run_set(emb, business: str, name: str) -> dict:
    UB.DATA = (ROOT / "examples" / business).resolve()
    rows = [dict(r) for r in csv.DictReader(open(UB.DATA / f"{name}.csv", encoding="utf-8"))]
    kb, index, _ = UB.build(emb)
    sim = lambda part, texts: (emb.embed_docs_cached(texts) @ emb.embed([part], "query")[0]).tolist() if texts else []  # noqa: E731

    def search(q, on=None):
        f, p, d = index.search(q, emb.embed([q], "query")[0], **({"on": on} if on is not None else {}))
        d["embed_ms"] = d["total_ms"] = 0.0
        return f, p, d

    search.model = index.fact_model
    day = date.fromisoformat(UB.AS_OF)
    pipe = pipeline.Pipeline(tenant_id="business")
    res, stages, parity, claims_total, claims_removed, status_pairs, verified = [], {}, 0, 0, 0, [], 0
    for row in rows:
        r = pipe.run(row["question"], search, sim, as_of=day)
        base = answer.respond(row["question"], search, sim, None, 0.4, evidence.SEM_MIN, as_of=day)
        parity += r["outcome"] == base["outcome"]
        j = UB.judge(row, r)
        res.append(j)
        for k, v in r["execution"]["stage_ms"].items():
            stages.setdefault(k, []).append(v)
        cl = [c for c in r["claim_verification"]["claims"] if c["kind"] in ("fact", "calc")]
        claims_total += len(cl)
        claims_removed += sum(c["status"] == "removed" for c in cl)
        verified += bool(r["verified"])
        status_pairs.append((row["expected"], r["sufficiency"]["status"], row["category"]))
    summ = UB.summarise(rows, res)
    keep = ("questions", "correct_answer_rate", "false_answer_rate", "citation_correct", "abstention_precision", "abstention_recall", "unnecessary_abstention_rate", "partial_answer_correctness", "conflict_detected")
    out = {k: summ.get(k) for k in keep}
    suff = {}
    for status in ("SUFFICIENT", "PARTIAL", "CONFLICTED", "INSUFFICIENT"):
        pred = [e for e, s, _ in status_pairs if s == status or (status == "INSUFFICIENT" and s == "AMBIGUOUS")]
        gold = [e for e, s, _ in status_pairs if status in {x for x in STATUS.get(e, set())} or (status == "INSUFFICIENT" and e in ("insufficient", "clarification"))]
        tp = sum(status in STATUS.get(e, set()) or (status == "INSUFFICIENT" and s == "AMBIGUOUS" and e in ("insufficient", "clarification")) for e, s, _ in status_pairs if (s == status or (status == "INSUFFICIENT" and s == "AMBIGUOUS")))
        suff[status] = {"precision": round(tp / len(pred), 3) if pred else None, "recall": round(tp / len(gold), 3) if gold else None, "predicted": len(pred), "expected": len(gold)}
    out["sufficiency"] = suff
    out["unsupported_claim_rate"] = round(claims_removed / claims_total, 3) if claims_total else 0.0
    out["claims_checked"] = claims_total
    out["verified_answers"] = f"{verified}/{len(rows)}"
    out["outcome_same_as_engine"] = f"{parity}/{len(rows)}"
    idx = [i for i, (_, _, c) in enumerate(status_pairs) if c in STRUCTURED]
    out["structured_correctness"] = f"{sum(res[i]['correct'] for i in idx)}/{len(idx)}"
    out["median_stage_ms"] = {k: round(statistics.median(v), 1) for k, v in stages.items()}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", nargs="*", default=["reasoning:dev", "reasoning:regression", "reasoning:fresh", "unified:dev", "unified:regression", "unified:fresh"])
    ap.add_argument("--json")
    args = ap.parse_args()
    emb = Embedder(ROOT / "hosted_models" / "minilm-l6" / "model")
    report = {"labelled_cases": labelled_cases(), "sets": {}}
    for s in args.sets:
        business, name = s.split(":")
        if name in ("locked", "verify"):
            sys.exit("the locked and verify sets are not run here")
        report["sets"][s] = run_set(emb, business, name)
    print(json.dumps(report, indent=1))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
