"""THE FAST PATH AGAINST THE DETERMINISTIC BASELINE: how many questions skip the model, whether what they skip to is correct and cited, and what it saves.

    .venv/bin/python scripts/fast_path_report.py [--sets dev regression fresh] [--model llama3.2:1b] [--out runs/fast-path/report.json]

For every question of two fictional businesses' development, regression and fresh sets (the sealed locked, verify and holdout sets are not choices here and are never read):
  * BASELINE: the existing deterministic pipeline (`Pipeline("deterministic")`), scored by the project's own judge (scripts/unified_benchmark.judge);
  * FAST PATH: the same stages, then `evaluate_fast_path`: taken (and why) or declined (and why). A taken answer is compared with the baseline's answer, sentence for sentence;
  * the numbers that matter for a SKIP: of the questions that skip the model, how many are correct, how many are false answers (given, and wrong), how many cite what they should, how long they take,
    and how many are answered text-for-text identically to the baseline (they must all be: the fast path only chooses not to ask).
With --model (an Ollama tag that is already installed; nothing is downloaded) the same questions that took the fast path are also sent through the local model with the fast path OFF, so that what was
saved can be seen: the model's time, and whether its answer was better, equal or worse than the exact answer it replaces.
The development sets were used to design the rule; the fresh sets are model-selection data and were NOT used to choose any condition (one fresh question was looked at, once, to understand a failure;
the finding is in docs/fast-path.md). Nothing here is a claim about accuracy on other businesses.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

BUSINESSES = {"acme": "examples/unified", "beacon": "examples/reasoning"}
SETS = ("dev", "regression", "fresh")  # the only sets this script can read


class AllowAll:
    def allows(self, tenant_id, provider_name):
        return True


def build(data: str):
    import unified_benchmark as UB
    from faqengine.runtime.embedder import Embedder

    UB.DATA = (ROOT / data).resolve()
    emb = Embedder(ROOT / "hosted_models" / "minilm-l6" / "model")
    kb, index, _ = UB.build(emb)

    def search(q, on=None):
        f, p, d = index.search(q, emb.embed([q], "query")[0], **({"on": on} if on is not None else {}))
        d["embed_ms"] = d["total_ms"] = 0.0
        return f, p, d

    search.model = index.fact_model
    return UB, search, lambda part, texts: (emb.embed_docs_cached(texts) @ emb.embed([part], "query")[0]).tolist() if texts else []


def judged(UB, row, r):
    return UB.judge(row, {"outcome": r["outcome"], "answer": r.get("answer"), "partial_answer": r.get("partial_answer"), "message": r.get("message") or "", "sources": r.get("sources") or [],
                          "conflicts": r.get("conflicts") or [], "retrieval": {"candidates": (r.get("retrieval") or {}).get("candidates", [])}})


def pct(a, b):
    return None if not b else round(a / b, 3)


def med(xs):
    return round(statistics.median(xs), 1) if xs else None


def summarise(rows, results):
    """The deterministic baseline's headline numbers for a group of questions, as the project's benchmark defines them."""
    n = len(rows)
    given = [i for i, j in enumerate(results) if j["answered"]]
    cite_rows = [i for i, r in enumerate(rows) if r["expected"] in ("answered", "partial", "conflict")]
    return {"questions": n, "correct": pct(sum(j["correct"] for j in results), n), "false_answer_rate": pct(sum(not results[i]["correct"] for i in given), len(given)), "answers_given": len(given),
            "citation_correct": pct(sum(results[i]["cite_ok"] for i in cite_rows), len(cite_rows))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", nargs="+", default=list(SETS), choices=SETS)
    ap.add_argument("--model", help="an installed Ollama tag: also run the fast-path questions through this model with the fast path off")
    ap.add_argument("--ollama-base", default="http://127.0.0.1:11434")
    ap.add_argument("--out", default="runs/fast-path/report.json")
    args = ap.parse_args(argv)

    from faqengine.runtime.pipeline import Pipeline

    report: dict = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sets": args.sets, "per_set": {}, "questions": []}
    provider = None
    if args.model:
        from faqengine.inference import registry

        provider = registry.build("ollama", args.model, "local", base_url=args.ollama_base, allow_private=True, timeout=300.0, structured="json_schema")
        report["model"] = args.model
    all_rows, all_base, taken_rows, taken_res, taken_ms, base_ms, eval_ms = [], [], [], [], [], [], []
    reasons: dict[str, int] = {}
    for biz, data in BUSINESSES.items():
        UB, search, sim = build(data)
        day = date.fromisoformat(UB.AS_OF)
        for setname in args.sets:
            rows = [dict(r) for r in csv.DictReader(open(UB.DATA / f"{setname}.csv", encoding="utf-8"))]
            base_j = []
            n_taken = 0
            for row in rows:
                q = row["question"]
                t0 = time.perf_counter()
                baseline = Pipeline("deterministic").run(q, search, sim, as_of=day)
                b_ms = (time.perf_counter() - t0) * 1000
                bj = judged(UB, row, baseline)
                base_j.append(bj)
                pipe = Pipeline("byok", provider, AllowAll(), tenant_id=biz, fast_path=True)
                t0 = time.perf_counter()
                st = pipe.prepare(q, search, sim, as_of=day)
                t1 = time.perf_counter()
                d = pipe.evaluate_fast_path(st)
                t2 = time.perf_counter()
                entry = {"business": biz, "set": setname, "id": row["id"], "category": row["category"], "expected": row["expected"], "taken": d.taken, "reason": d.reason, "baseline_outcome": baseline["outcome"],
                         "baseline_correct": bj["correct"], "baseline_cite_ok": bj["cite_ok"], "baseline_ms": round(b_ms, 1)}
                key = ":".join(d.reason.split(":")[:2]) if d.reason.startswith(("declined", "not_eligible")) else d.reason
                reasons[key] = reasons.get(key, 0) + 1
                if d.taken:
                    n_taken += 1
                    fast = pipe.conclude_fast(st, d)
                    total_ms = (time.perf_counter() - t0) * 1000
                    fj = judged(UB, row, fast)
                    entry.update({"fast_correct": fj["correct"], "fast_cite_ok": fj["cite_ok"], "fast_ms": round(total_ms, 1), "decision_ms": round((t2 - t1) * 1000, 1), "same_text_as_baseline": fast["answer"] == baseline["answer"] and
                                  [s["source"] for s in fast["sources"]] == [s["source"] for s in baseline["sources"]], "answer_path": fast["execution"]["answer_path"], "fast_path_reason": fast["execution"]["fast_path_reason"],
                                  "model_invoked": fast["execution"]["model_invoked"], "verified": fast["verified"], "evidence_ids": fast["execution"]["evidence_ids"]})
                    taken_rows.append(row)
                    taken_res.append(fj)
                    taken_ms.append(total_ms)
                    eval_ms.append((t2 - t1) * 1000)
                    base_ms.append(b_ms)
                    if provider is not None and d.reason == "exact_faq_direct":  # the same question, with the fast path OFF, through the real local model (only here does the model make a difference: a structured answer was never sent to one)
                        mp = Pipeline("local", provider, AllowAll(), tenant_id=biz, fast_path=False)
                        t3 = time.perf_counter()
                        m = mp.run(q, search, sim, as_of=day)
                        m_ms = (time.perf_counter() - t3) * 1000
                        mj = judged(UB, row, m)
                        entry["model"] = {"ms": round(m_ms, 1), "generated_by": m["execution"]["generated_by"], "outcome": m["outcome"], "correct": mj["correct"], "cite_ok": mj["cite_ok"], "verified": m["verified"],
                                          "failure_kind": m["execution"]["failure_kind"], "tokens_out": m["execution"]["usage"]["tokens_out"]}
                all_rows.append(row)
                all_base.append(bj)
                report["questions"].append(entry)
            report["per_set"][f"{biz}/{setname}"] = {"questions": len(rows), "fast_path_taken": n_taken, "baseline": summarise(rows, base_j)}

    taken = [e for e in report["questions"] if e["taken"]]
    wrong = [e for e in taken if not e["fast_correct"]]
    report["baseline_all_questions"] = summarise(all_rows, all_base)
    report["fast_path"] = {
        "questions": len(all_rows), "taken": len(taken), "coverage": pct(len(taken), len(all_rows)),
        "identical_to_the_baseline_answer": sum(e["same_text_as_baseline"] for e in taken), "all_identical": all(e["same_text_as_baseline"] for e in taken),
        "correct": sum(e["fast_correct"] for e in taken), "precision": pct(sum(e["fast_correct"] for e in taken), len(taken)), "false_answers": len(wrong), "false_answer_rate": pct(len(wrong), len(taken)),
        "citation_correct": pct(sum(e["fast_cite_ok"] for e in taken if e["expected"] in ("answered", "partial", "conflict")), sum(1 for e in taken if e["expected"] in ("answered", "partial", "conflict"))),
        "all_verified": all(e["verified"] for e in taken), "model_invoked": sum(e["model_invoked"] for e in taken),
        "wrong_ones": [{k: e[k] for k in ("business", "set", "id", "reason")} for e in wrong],
        "by_kind": {k: sum(e["fast_path_reason"] == k for e in taken) for k in ("exact_faq_direct", "structured_computed")},
        "latency_ms": {"fast_path_median": med(taken_ms), "fast_path_p95": round(sorted(taken_ms)[int(len(taken_ms) * 0.95)], 1) if taken_ms else None, "deterministic_pipeline_median": med(base_ms), "decision_overhead_median": med(eval_ms)},
    }
    by_set: dict = {}
    for e in taken:
        k = f"{e['business']}/{e['set']}"
        by_set.setdefault(k, {"taken": 0, "correct": 0, "exact_faq": 0})
        by_set[k]["taken"] += 1
        by_set[k]["correct"] += int(e["fast_correct"])
        by_set[k]["exact_faq"] += int(e["fast_path_reason"] == "exact_faq_direct")
    report["fast_path"]["by_set"] = by_set
    report["decisions"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1]))
    if provider is not None:
        ms = [e["model"] for e in taken if "model" in e]
        report["against_the_model"] = {
            "questions": len(ms), "model_median_ms": med([m["ms"] for m in ms]), "model_p95_ms": round(sorted(m["ms"] for m in ms)[int(len(ms) * 0.95)], 1) if ms else None,
            "model_answered_and_verified": sum(m["generated_by"] == "model" for m in ms), "model_fell_back_to_deterministic": sum(m["generated_by"] == "deterministic_fallback" for m in ms),
            "model_outcome_not_the_exact_answer": sum(m["outcome"] not in ("faq", "grounded") for m in ms), "model_correct": sum(m["correct"] for m in ms), "model_false_answers": sum(1 for m in ms if m["outcome"] in ("faq", "grounded") and not m["correct"]),
            "fast_path_correct_on_the_same_questions": sum(e["fast_correct"] for e in taken if "model" in e), "model_citation_ok": sum(m["cite_ok"] for m in ms),
            "milliseconds_saved_per_question_median": med([e["model"]["ms"] - e["fast_ms"] for e in taken if "model" in e])}
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    f, b = report["fast_path"], report["baseline_all_questions"]
    print(f"baseline (deterministic pipeline), {b['questions']} questions: correct {b['correct']}, false-answer rate {b['false_answer_rate']}, citation correct {b['citation_correct']}")
    print(f"fast path: taken {f['taken']}/{f['questions']} ({f['coverage']}); correct {f['correct']}/{f['taken']} (false answers {f['false_answers']}); citation correct {f['citation_correct']}; identical to the baseline's answer: {f['all_identical']}; model invoked: {f['model_invoked']}")
    print(f"latency: fast path median {f['latency_ms']['fast_path_median']} ms (p95 {f['latency_ms']['fast_path_p95']}), the deterministic pipeline {f['latency_ms']['deterministic_pipeline_median']} ms, the decision itself {f['latency_ms']['decision_overhead_median']} ms")
    if "against_the_model" in report:
        print("against the model:", json.dumps(report["against_the_model"]))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
