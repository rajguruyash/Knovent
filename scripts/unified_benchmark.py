"""Measures the unified knowledge engine on the Acme Cloud business (examples/unified): every kind of source at once, a customer's question, an answer that cites its sources.

    python scripts/unified_benchmark.py --set dev [--detail] [--json out.json]
    python scripts/unified_benchmark.py --set regression
    python scripts/unified_benchmark.py --set locked        # aggregate numbers only; run it once at the end, never tune on it
    UNIFIED_SRC=/path/to/another/src python scripts/unified_benchmark.py ...      # the same measurement of another version of the code (the "before")

Same path as the hosted service: each file is read on its own (a file the reader cannot ingest is a RESULT, listed, not a crash), all entries and their views are embedded with the
pretrained MiniLM, searched as one index, assessed, and composed with no language model. Five things are reported separately:

  retrieval          do the files that hold the evidence appear among the candidates? (all of them, for a question that needs several)
  sufficiency        does it answer (or answer in part) exactly the questions that have an answer? (precision and recall)
  correctness        right kind of response AND the facts that must be there are, and the ones that must not be, are not
  citation           the answer cites every file its facts come from, and none that must not be used (an expired or superseded one)
  abstention         does it decline exactly the questions that cannot be answered? (precision and recall)
Also: false answers (given, and wrong), conflicts found, outdated material used, files that could not be read.
"""
from __future__ import annotations

import argparse
from datetime import date
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("UNIFIED_SRC") or str(ROOT / "src"))

from faqengine import sources  # noqa: E402
from faqengine.kb import KB  # noqa: E402
from faqengine.runtime import answer, evidence  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402
from faqengine.runtime.retrieval import HybridIndex  # noqa: E402

DATA = ROOT / "examples" / "unified"
AS_OF = "2026-06-01"  # a fixed day, so that "expired" means the same thing every time
ANSWERED, ABSTAIN = {"faq", "grounded"}, {"insufficient", "clarification"}
EXPECT = {"answered": ANSWERED, "partial": {"partial"}, "conflict": {"conflict"}, "insufficient": ABSTAIN, "clarification": {"clarification"}}


def load_knowledge(folder: Path, as_of: str = AS_OF):
    entries, failures, files = [], [], sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in sources.SUPPORTED)
    seen: set[str] = set()
    for p in files:
        try:
            kb = sources.load_sources([p])
        except Exception as e:  # noqa: BLE001 - a file that cannot be ingested is a measurement, not a crash
            failures.append({"file": p.name, "error": str(e)[:140]})
            continue
        for e in kb.entries:
            if e.id in seen:
                e.id = f"{sources.slug(p.stem)}:{e.id}"
            seen.add(e.id)
            entries.append(e)
    resolve = getattr(sources, "resolve_validity", None)  # the validity rules exist only in the newer code
    if resolve:
        resolve(entries, as_of)
    return entries, failures


def entry_dict(e) -> dict:
    d = {"id": e.id, "question": e.question, "answer": e.answer, "kind": e.kind, "source": e.source, "category": e.category}
    for k in ("inactive", "valid_from", "valid_until", "heading_path", "section", "record"):
        if getattr(e, k, None) is not None:
            d[k] = getattr(e, k)
    return d


def build(embedder: Embedder):
    entries, failures = load_knowledge(DATA / "knowledge")
    kb = KB(entries)
    texts = [v.text for v in kb.views]
    vecs = embedder.embed(texts, "doc")
    index = HybridIndex([entry_dict(e) for e in kb.entries], [{"text": v.text, "entry": v.entry, "kind": v.kind} for v in kb.views], vecs)
    return kb, index, failures


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=\])\s+|(?<=[.!?])\s+(?=[A-Z])|\n+", text) if s.strip()]  # a sentence ends after its reference: "Fact. [1] Next fact. [2]"


def cited(r: dict) -> list[str]:
    out = [s.get("source", "") + " " + str(s.get("location", "")) for s in r.get("sources", []) or []]
    for c in r.get("conflicts", []) or []:
        out += [f"{x.get('source', '')} {x.get('location', '')}" for x in c.get("sources", [])] or list(c.get("between", []))
    return out


def judge(row: dict, r: dict) -> dict:
    expected, outcome = row["expected"], r["outcome"]
    text = (r.get("answer") or r.get("partial_answer") or r.get("message") or "").lower()
    contain = [x for x in row["must_contain"].lower().split(";") if x]
    forbid = [x for x in row["must_not_contain"].lower().split(";") if x]
    cites = [c.lower() for c in cited(r)]
    kind_ok = outcome in EXPECT[expected]
    facts_ok = all(c in text for c in contain) and not any(f in text for f in forbid) if expected in ("answered", "partial", "insufficient") else True
    groups = [g for g in row["cite"].lower().split(";") if g]
    cite_ok = all(any(alt in c for c in cites for alt in g.split("|")) for g in groups) and not any(f in c for f in row["forbid_cite"].lower().split(";") if f for c in cites)
    gold = [g for g in row["gold"].lower().split(";") if g]
    cand = [(c.get("source") or "").lower() for c in (r.get("retrieval") or {}).get("candidates", [])]
    got = sum(any(g in c for c in cand) for g in gold)
    traceable = None
    if outcome in ("grounded", "faq") and r.get("answer"):
        traceable = outcome == "faq" or all(re.search(r"\[\d+\]", s) for s in sentences(r["answer"]))
    return {"kind_ok": kind_ok, "facts_ok": facts_ok, "correct": kind_ok and facts_ok, "cite_ok": cite_ok, "gold": len(gold), "gold_found": got, "traceable": traceable,
            "answered": outcome in ANSWERED, "partial": outcome == "partial", "abstained": outcome in ABSTAIN, "conflict": outcome == "conflict", "outcome": outcome,
            "outdated_used": bool(row["forbid_cite"] and any(f in c for f in row["forbid_cite"].lower().split(";") if f for c in cites)) or any(f in text for f in forbid)}


def pct(a: int, b: int):
    return None if not b else round(a / b, 3)


def summarise(rows: list[dict], res: list[dict]) -> dict:
    n = len(rows)
    ans_exp = [i for i, r in enumerate(rows) if r["expected"] in ("answered", "partial")]
    abst_exp = [i for i, r in enumerate(rows) if r["expected"] in ABSTAIN or r["expected"] == "insufficient"]
    pred_ans = [i for i, j in enumerate(res) if j["answered"] or j["partial"]]
    pred_abs = [i for i, j in enumerate(res) if j["abstained"]]
    cite_rows = [i for i, r in enumerate(rows) if r["expected"] in ("answered", "partial", "conflict")]
    gold_rows = [i for i, j in enumerate(res) if j["gold"]]
    given = [i for i, j in enumerate(res) if j["answered"]]
    out = {"questions": n,
           "retrieval_evidence_recall": pct(sum(res[i]["gold_found"] for i in gold_rows), sum(res[i]["gold"] for i in gold_rows)),
           "retrieval_all_gold_found": pct(sum(res[i]["gold_found"] == res[i]["gold"] for i in gold_rows), len(gold_rows)),
           "sufficiency_precision": pct(len(set(pred_ans) & set(ans_exp)), len(pred_ans)), "sufficiency_recall": pct(len(set(pred_ans) & set(ans_exp)), len(ans_exp)),
           "correct_answer_rate": pct(sum(res[i]["correct"] for i in range(n)), n),
           "correct_when_answerable": pct(sum(res[i]["correct"] for i in ans_exp), len(ans_exp)),
           "false_answer_rate": pct(sum(not res[i]["correct"] for i in given), len(given)),
           "citation_correct": pct(sum(res[i]["cite_ok"] for i in cite_rows), len(cite_rows)),
           "citation_correct_when_answer_correct": pct(sum(res[i]["cite_ok"] and res[i]["correct"] for i in cite_rows), sum(res[i]["correct"] for i in cite_rows)),
           "answers_with_every_sentence_cited": pct(sum(1 for j in res if j["traceable"]), sum(1 for j in res if j["traceable"] is not None)),
           "abstention_precision": pct(len(set(pred_abs) & set(abst_exp)), len(pred_abs)), "abstention_recall": pct(len(set(pred_abs) & set(abst_exp)), len(abst_exp)),
           "conflict_detected": pct(sum(res[i]["conflict"] for i, r in enumerate(rows) if r["expected"] == "conflict"), sum(r["expected"] == "conflict" for r in rows)),
           "outdated_material_used": sum(j["outdated_used"] for j in res)}
    by_cat: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        by_cat.setdefault(r["category"], []).append(i)
    ans_or_part = [i for i, r in enumerate(rows) if r["expected"] in ("answered", "partial")]
    partial_rows = [i for i, r in enumerate(rows) if r["expected"] == "partial"]
    out["correct_abstention_rate"] = pct(sum(res[i]["abstained"] for i in abst_exp), len(abst_exp))  # of the questions that cannot be answered, the share it declined
    out["unnecessary_abstention_rate"] = pct(sum(res[i]["abstained"] for i in ans_or_part), len(ans_or_part))  # of the questions that can be answered (fully or in part), the share it declined
    out["partial_answer_correctness"] = pct(sum(res[i]["correct"] for i in partial_rows), len(partial_rows))
    out["by_category_correct"] = {k: f"{sum(res[i]['correct'] for i in v)}/{len(v)}" for k, v in by_cat.items()}
    out["by_category_citation"] = {k: f"{sum(res[i]['cite_ok'] for i in v if rows[i]['expected'] in ('answered', 'partial', 'conflict'))}/{sum(rows[i]['expected'] in ('answered', 'partial', 'conflict') for i in v)}"
                                   for k, v in by_cat.items() if any(rows[i]["expected"] in ("answered", "partial", "conflict") for i in v)}
    by_type: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        for t in [t for t in r["source_types"].split(";") if t]:
            by_type.setdefault(t, []).append(i)
    out["by_source_type_correct"] = {k: f"{sum(res[i]['correct'] for i in v)}/{len(v)}" for k, v in sorted(by_type.items())}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default="dev", choices=["dev", "regression", "locked", "verify", "fresh"])
    ap.add_argument("--data", default=None, help="another business folder with knowledge/ and dev.csv, regression.csv (default examples/unified); also UNIFIED_DATA")
    ap.add_argument("--detail", action="store_true", help="print every question (refused for the locked set)")
    ap.add_argument("--json")
    args = ap.parse_args()
    global DATA
    if args.data or os.environ.get("UNIFIED_DATA"):
        DATA = (ROOT / (args.data or os.environ["UNIFIED_DATA"])).resolve()
    if args.set in ("locked", "verify") and args.detail:
        sys.exit("the locked and verify sets report aggregate numbers only: looking at its questions one by one is how a benchmark stops being independent")
    rows = [dict(r) for r in csv.DictReader(open(DATA / f"{args.set}.csv", encoding="utf-8"))]
    emb = Embedder(ROOT / "hosted_models" / "minilm-l6" / "model")
    kb, index, failures = build(emb)
    sim = lambda part, texts: (emb.embed_docs_cached(texts) @ emb.embed([part], "query")[0]).tolist() if texts else []  # noqa: E731

    def search(q: str, on=None):
        started = time.perf_counter()
        faq, passages, diag = index.search(q, emb.embed([q], "query")[0], **({"on": on} if on is not None else {}))
        diag["embed_ms"] = 0.0
        diag["total_ms"] = (time.perf_counter() - started) * 1000
        return faq, passages, diag

    if hasattr(index, "fact_model"):  # the structured reasoning exists only in the newer code
        search.model = index.fact_model
    res = []
    for row in rows:
        r = answer.respond(row["question"], search, sim, None, 0.4, evidence.SEM_MIN, **({"as_of": date.fromisoformat(AS_OF)} if "as_of" in answer.respond.__code__.co_varnames else {}))
        j = judge(row, r)
        res.append(j)
        if args.detail:
            text = (r.get("answer") or r.get("partial_answer") or r.get("message") or "")[:110]
            print(f"{'ok ' if j['correct'] and (j['cite_ok'] or row['expected'] in ('insufficient', 'clarification')) else 'BAD'} [{row['category']:12s}] {row['question'][:58]:58s} -> {j['outcome']:12s} cite={'y' if j['cite_ok'] else 'N'} {text!r}")
    summary = summarise(rows, res)
    summary["ingestion_failures"] = failures
    summary["entries"] = len(kb.entries)
    summary["vectors"] = len(kb.views)
    print(json.dumps(summary, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
