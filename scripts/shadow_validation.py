#!/usr/bin/env python3
"""A controlled shadow-mode test of the hosted service on SYNTHETIC traffic, with a report.

    python scripts/shadow_validation.py [--json runs/shadow-validation/report.json] [--markdown docs/shadow-validation.md] [--limit N]

WHAT IT IS. It builds a throwaway local PostgreSQL with every migration, ingests several fictional businesses through the hosted service (the real embedding model, stored passages and vectors),
and sends synthetic requests. After one warm-up pass (not counted), EVERY request is sent twice, once with ENGINE_PIPELINE=off and once on `shadow`, in an order chosen at random for each
request, so that slow drift in this machine (heat, other work) falls on both alike and the difference per request is what is measured. In `shadow` the existing path answers, the pipeline runs
beside it on the same request, and every difference is recorded exactly as the live service would record it (`audit_logs`, `pipeline.shadow_mismatch`).

WHAT IT IS NOT. Not real customer traffic: every question is one the repository's authors wrote or derived from its own example knowledge. Not a Supabase project: a local PostgreSQL. Not a
benchmark of answer quality (nothing is scored right or wrong; only whether the two paths AGREE). The report says all of this in its first lines.

TRAFFIC. Each business is asked its own development and regression questions, three phrasings of each (as written; lower case without punctuation; wrapped in a greeting), and a sample of the
OTHER businesses' questions (which it must not answer from anyone else's knowledge: every source of every answer is checked to be its own). The locked, fresh, verify and held-out sets are
never opened: the script refuses any file named for them.
"""
from __future__ import annotations

import argparse
import csv
import json
import platform
import random
import re
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
EX = ROOT / "examples"
FORBIDDEN = re.compile(r"locked|fresh|verify|held[-_]?out", re.I)
DAY = date(2026, 6, 1)  # the day the example sets were written for
MODEL = ROOT / "dist" / "bike-shop" / "model"


def read_rows(path: Path):
    if FORBIDDEN.search(path.name):
        raise SystemExit(f"refusing to open {path.name}: it is a frozen evaluation set")
    return [dict(r) for r in csv.DictReader(open(path, encoding="utf-8"))]


def knowledge_files(root: Path) -> list[Path]:
    from faqengine import sources

    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in sources.SUPPORTED and not FORBIDDEN.search(p.name)]


def tenants() -> list[dict]:
    """Fictional businesses: their files, and the questions that belong to them."""
    uni = [r["question"] for n in ("dev", "regression") for r in read_rows(EX / "unified" / f"{n}.csv")]
    rea = [r["question"] for n in ("dev", "regression") for r in read_rows(EX / "reasoning" / f"{n}.csv")]
    evi = [r["question"] for n in ("dev", "regression") for r in read_rows(EX / "evidence" / f"{n}.csv")]
    bike = [r["query"] for r in read_rows(EX / "bike-shop" / "dev.csv")] + [r["query"] for r in read_rows(EX / "bike-shop" / "regression.csv") if r.get("split") == "dev"]
    out = [
        {"name": "acme", "files": knowledge_files(EX / "unified" / "knowledge"), "questions": uni},
        {"name": "beacon", "files": knowledge_files(EX / "reasoning" / "knowledge"), "questions": rea},
        {"name": "pawfect", "files": [EX / "evidence" / n for n in ("pawfect_faq.csv", "pawfect_handbook.md", "policy_update.md")], "questions": evi},
        {"name": "bike", "files": [EX / "bike-shop" / "faq.csv"], "questions": bike},
    ]
    for d in ("clinic", "restaurant", "saas"):  # businesses with only a FAQ: asked their own FAQ questions (known answers) in three phrasings
        faq = EX / "domains" / d / "faq.csv"
        out.append({"name": d, "files": [faq], "questions": [r["question"] for r in read_rows(faq)][:30]})
    out.append({"name": "empty", "files": [], "questions": []})  # a business with no knowledge at all
    return out


def phrasings(q: str) -> list[str]:
    plain = re.sub(r"\s+", " ", re.sub(r"[?!.,;:]+", " ", q.lower())).strip()
    return list(dict.fromkeys([q, plain, f"Hi, {q[0].lower() + q[1:]} Thanks!"]))


class Files(dict):
    def __call__(self, path):
        return self[path]


def build(limit: int | None):
    import base64

    from faqengine.hosted import service as svc
    from faqengine.hosted.secrets import SecretBox
    from faqengine.hosted.store import Store
    from faqengine.runtime.embedder import Embedder
    from pgdb import make_db

    try:
        gen = make_db()
        db = next(gen)
    except BaseException as e:  # noqa: BLE001 - pgdb skips with pytest's Skipped (a BaseException) when there is no local PostgreSQL
        raise SystemExit(f"no local PostgreSQL with pgvector to test on: {e}") from None
    clock = {"day": DAY}
    box = SecretBox({1: base64.b64decode(SecretBox.generate())})
    engine = svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), rate_per_minute=10**9, today=lambda: clock["day"], shadow_records_per_hour=10**9)
    world = []
    for t in tenants():
        owner = str(db.user(f"sv-{t['name']}"))
        biz = str(db.business(owner, f"sv-{t['name']}"))
        names = set()
        for p in t["files"]:
            kind = "faq_table" if p.name == "faq.csv" else "document"
            engine.fetch_object[f"{biz}/{p.name}"] = p.read_bytes()
            sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, %s, %s, %s) returning id", (biz, p.name, kind, f"{biz}/{p.name}"))[0][0])
            engine.ingest(biz, owner, sid)
            names.add(p.name)
        qs = t["questions"][:limit] if limit else t["questions"]
        world.append({**t, "owner": owner, "biz": biz, "sources": names, "questions": qs})
    return gen, db, engine, world


def traffic(world: list[dict], seed: int = 20261102) -> list[dict]:
    rng = random.Random(seed)
    reqs = []
    for t in world:
        for q in t["questions"]:
            for ph in phrasings(q):
                reqs.append({"tenant": t["name"], "question": ph, "kind": "own"})
        others = [(o["name"], q) for o in world if o is not t for q in o["questions"]]
        for name, q in rng.sample(others, min(len(others), 12 + len(t["questions"]) // 4)):
            reqs.append({"tenant": t["name"], "question": q, "kind": "foreign", "from": name})
    rng.shuffle(reqs)  # interleaved: no business is warmed or cooled by the order
    return reqs


def run_pass(engine, world, reqs, mode: str) -> dict:
    """One plain pass in one mode (used for the warm-up)."""
    engine.pipeline_mode = mode
    for r in reqs:
        t = next(x for x in world if x["name"] == r["tenant"])
        engine.respond(t["biz"], t["owner"], r["question"])
    return {}


def paired_pass(engine, world, reqs, seed: int = 7) -> dict:
    """Every request twice (off and shadow), the order chosen at random per request. Returns the times of each and the pairs."""
    rng = random.Random(seed)
    by = {t["name"]: t for t in world}
    known = {t["name"]: set(getattr(engine._tenant(t["biz"]), "db_ids", {})) for t in world}
    engine.stats.clear()
    for d in engine.latency.values():
        d.clear()
    from faqengine.runtime import shadow as sh_mod

    strict = Counter()  # what a comparison WITHOUT the two documented allowances would have flagged, by kind
    original = type(engine)._shadow

    def counting(self, business_id, user_id, q, legacy, new):
        found = sh_mod.compare(legacy, new, strict=True)
        if found:
            strict["requests"] += 1
            for k in sh_mod.categories(found):
                strict[k] += 1
        return original(self, business_id, user_id, q, legacy, new)

    type(engine)._shadow = counting
    walls = {"off": [], "shadow": []}
    outcomes = {"off": Counter(), "shadow": Counter()}
    pairs, leaks, rows = [], [], []
    for r in reqs:
        t = by[r["tenant"]]
        got = {}
        for mode in rng.sample(["off", "shadow"], 2):
            engine.pipeline_mode = mode
            started = time.perf_counter()
            out = engine.respond(t["biz"], t["owner"], r["question"])["result"]
            got[mode] = (time.perf_counter() - started) * 1000
            walls[mode].append(got[mode])
            outcomes[mode][out["outcome"]] += 1
            mine = known[r["tenant"]]  # the entry ids of THIS business's index: nothing retrieved or cited may be outside it
            seen = {s.get("id") for s in out.get("sources") or []} | {c.get("entry_id") for c in (out.get("retrieval") or {}).get("candidates", [])}
            stray = {x for x in seen if x and x not in mine}
            if stray:
                leaks.append({"tenant": r["tenant"], "mode": mode, "stray_entries": sorted(stray)[:5]})
            rows.append((r["tenant"], [s.get("db_id") for s in out.get("sources") or [] if s.get("db_id")]))
        pairs.append((got["off"], got["shadow"]))
    type(engine)._shadow = original
    return {"strict": dict(strict), "n": len(reqs), "walls": walls, "outcomes": {k: dict(v) for k, v in outcomes.items()}, "pairs": pairs, "leaks": leaks, "stats": dict(engine.stats), "latency": engine.metrics()["latency"], "sources": rows}


def summary(values: list[float]) -> dict:
    from faqengine.runtime import shadow

    return {"n": len(values), "median_ms": round(shadow.percentile(values, 50) or 0, 1), "p95_ms": round(shadow.percentile(values, 95) or 0, 1), "mean_ms": round(statistics.fmean(values), 1) if values else None}


def analyse(db, world, passed: dict, reqs) -> dict:
    from faqengine.runtime import shadow as sh_mod

    stats = passed["stats"]
    ran = stats.get("shadow_match", 0) + stats.get("shadow_mismatch", 0)
    records = []
    for t in world:
        for (detail,) in db.su("select detail from public.audit_logs where business_id = %s and action = 'pipeline.shadow_mismatch' order by created_at", (t["biz"],)):
            records.append({"tenant": t["name"], **detail})
    faults = [d for t in world for (d,) in db.su("select detail from public.audit_logs where business_id = %s and action = 'pipeline.error'", (t["biz"],))]
    kinds = Counter(k for r in records for k in r.get("categories", []))
    own = {t["name"]: t["biz"] for t in world}
    stray_rows, checked = 0, 0  # every source of every answer belongs to the business that asked: checked against the database, not against names
    for tenant, ids in passed["sources"]:
        for i in ids:
            checked += 1
            row = db.su("select business_id::text from public.faqs where id = %s union all select business_id::text from public.document_chunks where id = %s", (i, i))
            stray_rows += not row or row[0][0] != own[tenant]
    off, sh = passed["walls"]["off"], passed["walls"]["shadow"]
    deltas = [b - a for a, b in passed["pairs"]]
    third = max(1, len(deltas) // 3)
    drift = [round(sh_mod.percentile(deltas[i:i + third], 50) or 0, 1) for i in range(0, len(deltas) - third + 1, third)][:3]
    o, s_ = summary(off), summary(sh)
    over = {"paired_median_ms": round(sh_mod.percentile(deltas, 50) or 0, 1), "paired_p95_ms": round(sh_mod.percentile(deltas, 95) or 0, 1), "median_of_off_pct": round(100 * (sh_mod.percentile(deltas, 50) or 0) / o["median_ms"], 1),
            "p95_of_off_pct": round(100 * (s_["p95_ms"] / o["p95_ms"] - 1), 1), "paired_median_by_third_of_the_run_ms": drift}
    no_knowledge = sum(r["tenant"] in {t["name"] for t in world if not t["files"]} for r in reqs)
    per = {t["name"]: {"requests": sum(r["tenant"] == t["name"] for r in reqs)} for t in world}
    for r in records:
        per[r["tenant"]]["mismatches"] = per[r["tenant"]].get("mismatches", 0) + 1
    return {
        "requests_evaluated": passed["n"], "requests_to_a_business_with_no_knowledge": no_knowledge, "shadow_executions_ok": ran, "shadow_exceptions": stats.get("shadow_error", 0),
        "matches": stats.get("shadow_match", 0), "mismatches": stats.get("shadow_mismatch", 0), "mismatches_recorded_in_audit_log": len(records), "mismatch_kinds": dict(kinds),
        "answer_mismatches": kinds.get("answer", 0), "claim_mismatches": kinds.get("claims", 0), "citation_mismatches": kinds.get("citations", 0), "evidence_differences": kinds.get("evidence", 0),
        "abstention_differences": kinds.get("abstention", 0), "conflict_differences": kinds.get("conflict", 0), "structured_differences": kinds.get("structured", 0), "tenant_differences": kinds.get("tenant", 0),
        "fallback": {"pipeline_fallback": stats.get("pipeline_fallback", 0), "pipeline_errors_answered_by_existing_path": stats.get("pipeline_error", 0),
                     "frequency": round((stats.get("pipeline_fallback", 0) + stats.get("pipeline_error", 0)) / max(1, ran + stats.get("shadow_error", 0)), 4)},
        "latency": {"wall_clock_per_request": {"off": o, "shadow": s_}, "pipeline_own_time": passed["latency"]["pipeline"], "existing_path_own_time": passed["latency"]["legacy"], "overhead_of_shadow": over},
        "isolation": {"answer_sources_checked_against_the_database": checked, "sources_of_another_business": stray_rows,
                      "answers_retrieving_or_citing_an_entry_of_another_business": len(passed["leaks"])},
        "outcomes_off": passed["outcomes"]["off"], "outcomes_shadow": passed["outcomes"]["shadow"], "outcomes_identical_between_off_and_shadow": passed["outcomes"]["off"] == passed["outcomes"]["shadow"],
        "strict_comparison_would_have_flagged": passed["strict"], "per_business": per, "mismatch_records": records, "fault_records": faults,
    }


def render(rep: dict) -> str:
    a, meta = rep["analysis"], rep["meta"]
    lat = a["latency"]
    w = lat["wall_clock_per_request"]
    ov = lat["overhead_of_shadow"]
    L = ["# Shadow-mode validation (synthetic traffic)", "",
         "> **This is not customer traffic and not a Supabase project.** Every request is a question written by this repository's authors or derived from its own fictional example knowledge, sent to the hosted",
         "> service running on a throwaway local PostgreSQL. It shows whether the two answer paths AGREE and what the pipeline costs on this machine. It says nothing about live traffic, about a Supabase",
         "> project, or about answer quality. `ENGINE_PIPELINE=on` was not enabled for anyone.", "",
         f"Run: {meta['started']} · {meta['machine']} · Python {meta['python']} · businesses: {', '.join(meta['businesses'])} · embedding model: the shared pretrained model (no tuning)", "",
         "## Result", "", "| Measure | Value |", "|---|---|",
         f"| Requests evaluated in `shadow` | **{a['requests_evaluated']}** ({a['requests_to_a_business_with_no_knowledge']} to a business with no knowledge, which neither path runs on) |", f"| Shadow executions that completed | **{a['shadow_executions_ok']}** |", f"| Shadow exceptions (the existing path answered) | **{a['shadow_exceptions']}** |",
         f"| Requests where both paths agreed on everything compared | **{a['matches']}** |", f"| Requests with at least one difference | **{a['mismatches']}** |",
         f"| …recorded in `audit_logs` (`pipeline.shadow_mismatch`) | {a['mismatches_recorded_in_audit_log']} |",
         f"| Answer differences / claim differences / citation differences | {a['answer_mismatches']} / {a['claim_mismatches']} / {a['citation_mismatches']} |",
         f"| Evidence differences (candidates, what was left out as not current) | {a['evidence_differences']} |",
         f"| Abstention / conflict / structured-result / tenant differences | {a['abstention_differences']} / {a['conflict_differences']} / {a['structured_differences']} / {a['tenant_differences']} |",
         f"| Fallback frequency (pipeline fell back or failed, per pipeline run) | {a['fallback']['frequency']:.2%} ({a['fallback']['pipeline_fallback']} fallbacks, {a['fallback']['pipeline_errors_answered_by_existing_path']} errors) |",
         f"| Outcome counts identical with the pipeline off and in shadow | {'yes' if a['outcomes_identical_between_off_and_shadow'] else '**NO**'} |",
         f"| Answer sources checked against the database | {a['isolation']['answer_sources_checked_against_the_database']} |",
         f"| …that belonged to another business | **{a['isolation']['sources_of_another_business']}** |",
         f"| Answers (including cross-business questions) that retrieved or cited an entry outside the asking business's own index | **{a['isolation']['answers_retrieving_or_citing_an_entry_of_another_business']}** |", "",
         "## Latency", "",
         "Wall clock of the whole request (retrieval, answer, and the writes of the exchange to the database), per request, milliseconds. Every request was sent in both modes, in a random order, so the",
         "overhead is the median and 95th percentile of the PER-REQUEST difference (shadow minus off); the two columns beside it are each mode's own median and p95.", "",
         "| | n | median | p95 |", "|---|---|---|---|",
         f"| `off` | {w['off']['n']} | {w['off']['median_ms']} | {w['off']['p95_ms']} |", f"| **`shadow`** | {w['shadow']['n']} | **{w['shadow']['median_ms']}** | **{w['shadow']['p95_ms']}** |", "",
         f"Overhead of running the pipeline beside the answer: **+{ov['paired_median_ms']} ms median ({ov['median_of_off_pct']:+.0f}% of the `off` median), +{ov['paired_p95_ms']} ms at the 95th percentile of the per-request difference**; p95 of `shadow` is {ov['p95_of_off_pct']:+.0f}% of p95 of `off`. Median difference in each third of the run (drift check): {', '.join(str(x) for x in ov['paired_median_by_third_of_the_run_ms'])} ms.",
         f"The pipeline's own time, inside `shadow`: median {lat['pipeline_own_time']['median_ms']} ms, p95 {lat['pipeline_own_time']['p95_ms']} ms (n={lat['pipeline_own_time']['n']}); the existing path's own time (both modes): median {lat['existing_path_own_time']['median_ms']} ms, p95 {lat['existing_path_own_time']['p95_ms']} ms.",
         "In this version the pipeline runs inside the request, after the answer is made, so a sampled request waits for it: the overhead above is added to what a customer waits for on every sampled request (`ENGINE_SHADOW_SAMPLE` chooses the share; set it low for a live test). This machine is a throttled 8 GB laptop; a server will differ.", "",
         "## Per business", "", "| Business | Requests | Differences |", "|---|---|---|"]
    for k, v in a["per_business"].items():
        L.append(f"| {k} | {v['requests']} | {v.get('mismatches', 0)} |")
    st = a["strict_comparison_would_have_flagged"]
    L += ["", "## What the two allowances do on this traffic", "",
          "The comparison allows exactly two things (docs/reasoning-pipeline.md): extra candidates added at the END of the list by the pipeline's one targeted second retrieval, and a conflict's two sides read as claims on both paths. "
          f"Without them the same traffic would have flagged **{st.get('requests', 0)}** of {a['shadow_executions_ok']} requests" + (f" ({', '.join(f'{k}: {v}' for k, v in st.items() if k != 'requests')})." if st.get("requests") else ".") +
          " Everything beyond them is in the table below: the allowances hide nothing else.", "", "## Differences, as recorded", ""]
    if not a["mismatch_records"]:
        L.append("None: no request had a difference in outcome, answer, claims, cited sources, conflicts, abstention, structured results, retrieved candidates or what was left out as not current.")
    else:
        L += ["Read from `audit_logs` (phrases and ids only; no question text and no document text is stored).", "", "| Business | Kinds | What differed | Existing → pipeline outcome |", "|---|---|---|---|"]
        for r in a["mismatch_records"]:
            L.append(f"| {r['tenant']} | {', '.join(r.get('categories', []))} | {'; '.join(r.get('differences', []))} | {r.get('legacy_outcome')} → {r.get('pipeline_outcome')} |")
    if a["fault_records"]:
        L += ["", "## Pipeline faults", "", *[f"* {f}" for f in a["fault_records"]]]
    L += ["", "## Reproduce", "", "```bash", "python scripts/shadow_validation.py --json runs/shadow-validation/report.json --markdown docs/shadow-validation.md", "```", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json")
    ap.add_argument("--markdown")
    ap.add_argument("--limit", type=int, help="at most this many questions per business (a quick look)")
    args = ap.parse_args()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    gen, db, engine, world = build(args.limit)
    try:
        reqs = traffic(world)
        print(f"{len(world)} businesses, {len(reqs)} synthetic requests per pass", flush=True)
        run_pass(engine, world, reqs, "off")  # warm-up: caches, indexes in memory; not counted
        passed = paired_pass(engine, world, reqs)
        print(f"  paired pass done: {len(reqs)} requests, each in both modes", flush=True)
        rep = {"meta": {"started": started, "machine": f"{platform.system()} {platform.machine()}", "python": platform.python_version(), "businesses": [t["name"] for t in world], "synthetic": True, "target": "local PostgreSQL (not Supabase)"},
               "analysis": analyse(db, world, passed, reqs)}
    finally:
        gen.close()
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str))
    md = render(rep)
    if args.markdown:
        Path(args.markdown).write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
