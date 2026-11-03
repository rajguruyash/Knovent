"""For every answerable held-out question the evidence-aware answer did not get right: the question, what was expected, what was retrieved, the similarity, the
sufficiency decision and the exact reason, and a classification of the failure.

    python scripts/analyze_misses.py --run runs/pawfect-heldout --heldout examples/pawfect/heldout.csv --kb examples/pawfect/pawfect_faq.csv examples/pawfect/pawfect_handbook.md \\
        [--impl before|after] [--out docs/evidence-miss-analysis.md]

Failure classes: retrieval failure (the expected entry was not among the candidates), paraphrase mismatch (it was, and says it in other words that the check did not
accept), missing evidence (it was, and does not contain the kind of detail asked for), partial evidence (several parts, some supported), sufficiency false negative (it
was, it contains the words and the detail, and the check still refused), response generation failure (the check accepted but the response was wrong or not composed).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_module(impl: str):
    if impl == "before":
        spec = importlib.util.spec_from_file_location("evidence_v1", ROOT / "scripts" / "legacy_evidence_v1.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["evidence_v1"] = mod
        spec.loader.exec_module(mod)
        return mod
    from faqengine.runtime import evidence

    return evidence


def main() -> None:
    from faqengine.heldout import OOS_TYPES, load_heldout
    from faqengine.kb import load_kb
    from faqengine.studio.engine import ProjectEngine
    from faqengine.studio.store import read_json

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--heldout", required=True)
    ap.add_argument("--kb", nargs="+", required=True)
    ap.add_argument("--impl", default="before", choices=["before", "after"])
    ap.add_argument("--out")
    ap.add_argument("--method", default="hybrid", choices=["hybrid", "semantic"], help="how the candidates are found (the analysis of the earlier milestone used the model alone)")
    args = ap.parse_args()
    ev = load_module(args.impl)
    kb = load_kb(args.kb)
    ho = load_heldout(args.heldout, kb)
    eng = ProjectEngine(args.run)
    th = read_json(Path(args.run) / "thresholds.json")
    sim = eng.sentence_similarity()
    sem_min = th.get("evidence_sim_min", ev.SEM_MIN)
    text_of = {e.id: e for e in kb.entries}

    rows, counts = [], {"answered correctly": 0, "correct abstention": 0, "fabricated answer": 0, "missed answer": 0, "answered from the wrong source": 0,
                        "stored FAQ answer for a document question": 0, "partial answer": 0}
    for i in range(len(ho)):
        q = ho.queries[i]
        gold = [kb.entries[g].id for g in sorted(ho.gold[i])]
        oos = ho.types[i] in OOS_TYPES
        faq, passages = eng.candidates(q, 8, args.method)
        r = ev.respond(q, faq, passages, sim=sim, sem_min=sem_min)
        answered = r["outcome"] in ("faq", "grounded")
        if r["outcome"] == "partial":
            counts["fabricated answer" if oos else "partial answer"] += 1
            if not oos:
                rows.append({"q": q, "type": ho.types[i], "expected": gold, "faq": [], "passages": [], "similarity": round(r["retrieval_similarity"], 2), "outcome": "partial", "sufficiency": round(r["answer_sufficiency"], 2),
                             "message": r.get("message", ""), "class": "partial evidence", "detail": []})
            continue
        cited = {x["id"] for x in r.get("sources", [])}
        if oos:
            counts["fabricated answer" if answered else "correct abstention"] += 1
            continue
        if answered and cited & set(gold):
            counts["answered correctly"] += 1
            continue
        if answered:
            counts["stored FAQ answer for a document question" if r["outcome"] == "faq" and all(text_of[g].kind == "passage" for g in gold) else "answered from the wrong source"] += 1
            kind = "response generation failure" if r["outcome"] == "grounded" else "retrieval failure"
        else:
            counts["missed answer"] += 1
            kind = None
        found = {h["entry_id"]: h for h in faq + passages}
        expected = [g for g in gold if g in found]
        parts = ev.split_parts(q)
        detail = []
        for part in parts:
            for g in expected or gold:
                e = text_of[g]
                topic = set(ev.terms(e.question))
                if hasattr(ev, "classify"):
                    c = ev.classify(part, e.answer, topic, sim, sem_min, float(found[g]["score"]) if g in found else 0.0)
                    detail.append({"part": part, "entry": g, "class": c["class"], "coverage": c["coverage"], "lexical_coverage": c["coverage"], "detail_kind_present": c["detail_ok"],
                                   "asks_for": c["asks_for"], "best_sentence": c["sentence"][:140], "semantic": c["semantic"] or 0.0, "why": c["reason"]})
                else:
                    res = ev._best_sentence(part, e.answer, topic, sim, sem_min)
                    raw_cover, raw_kind = ev.support(part, res[2], topic)
                    detail.append({"part": part, "entry": g, "class": "?", "coverage": round(res[0], 2), "lexical_coverage": round(raw_cover, 2), "detail_kind_present": res[1],
                                   "asks_for": ev.asks_for(part), "best_sentence": res[2][:140], "semantic": round(max(sim(part, [res[2]]) or [0]), 2), "why": ""})
        if kind is None:
            if not expected:
                kind = "retrieval failure"
            elif len(parts) > 1 and any(d["class"] in ("direct", "semantic") or (d["class"] == "?" and d["coverage"] >= ev.MIN_SUPPORT and d["detail_kind_present"]) for d in detail):
                kind = "partial evidence"
            elif any(d["asks_for"] and not d["detail_kind_present"] for d in detail):
                kind = "missing evidence"
            elif any(d["lexical_coverage"] >= 0.75 for d in detail):
                kind = "sufficiency false negative"
            elif any(d["semantic"] >= 0.3 or d["lexical_coverage"] > 0 for d in detail):
                kind = "paraphrase mismatch"
            else:
                kind = "missing evidence"
        rows.append({"q": q, "type": ho.types[i], "expected": gold, "faq": [(h["entry_id"], round(h["score"], 2)) for h in faq[:3]], "passages": [(h["entry_id"], round(h["score"], 2)) for h in passages[:3]],
                     "similarity": round(r["retrieval_similarity"], 2), "outcome": r["outcome"], "sufficiency": round(r["answer_sufficiency"], 2), "message": r.get("message", ""),
                     "class": kind, "detail": detail[:3]})

    by_class: dict[str, int] = {}
    for r in rows:
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    answerable = sum(counts[k] for k in ("answered correctly", "missed answer", "answered from the wrong source", "stored FAQ answer for a document question", "partial answer"))
    L = [f"# Evidence-aware answers: what went wrong ({args.impl} the evidence milestone)", "",
         f"Diagnostic set: {len(ho)} unique held-out questions from the Pawfect pack ({answerable} answerable, {counts['correct abstention'] + counts['fabricated answer']} that should be declined). "
         "This set has now been read question by question, so it is a development set, not a clean test.", "", "## Where every question went", ""]
    L += [f"- {k}: {v}" for k, v in counts.items() if v]
    L += ["", f"The \"unaccounted for\" questions are the ones that were answered but wrongly: {counts['answered from the wrong source']} answered from a source that was not expected and "
          f"{counts['stored FAQ answer for a document question']} given a stored FAQ answer where only the handbook holds the information"
          + (f", and {counts['partial answer']} answered only in part" if counts["partial answer"] else "") + f". They are neither correct nor missed, which is why correct + missed = {counts['answered correctly'] + counts['missed answer']} of {answerable}.", "", "## Failures by class", ""]
    L += [f"- {k}: {v}" for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])]
    L += ["", "## Every failure", ""]
    for n, r in enumerate(rows, 1):
        L += [f"### {n}. {r['q']}", f"- class: **{r['class']}** · outcome: {r['outcome']} · retrieval similarity {r['similarity']} · sufficiency {r['sufficiency']} · type {r['type']}",
              "- expected: " + "; ".join(f"{g} ({text_of[g].question}: {text_of[g].answer[:90]})" for g in r["expected"]),
              f"- retrieved FAQ: {r['faq']} · passages: {r['passages']}"]
        for d in r["detail"]:
            L.append(f"- check of \"{d['part']}\" against {d['entry']}: class {d['class']}, words covered {d['lexical_coverage']}, similarity of best sentence {d['semantic']}, asks for {d['asks_for'] or 'nothing specific'}, detail present: {d['detail_kind_present']}; {d['why']}; best sentence: \"{d['best_sentence']}\"")
        if r["message"]:
            L.append(f"- reason given: {r['message'][:200]}")
        L.append("")
    text = "\n".join(L)
    if args.out:
        Path(args.out).write_text(text)
    print("\n".join(L[:22]))


if __name__ == "__main__":
    main()
