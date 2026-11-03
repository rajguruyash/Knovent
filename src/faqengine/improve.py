"""Retrain a deployed package from its own source files plus every question it has learned, test the candidate against what is live,
and promote it only if it is not worse.

This is what `python ci/improve.py retrain` (inside an exported package) runs. It needs the faqengine tool; the package alone does not.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from . import metrics
from .auto import export_run, run_auto
from .heldout import IN_SCOPE_TYPES, load_heldout
from .kb import load_kb
from .runtime import versions

MIN_TEST = 20  # fewer held-out test questions than this cannot tell a better engine from a worse one


def learned_questions(package: Path, extra_file: Path | None = None) -> dict[str, list[str]]:
    """Every question this deployment learned (entry id -> questions): data/learned.jsonl, which only ever grows, and optionally a file
    downloaded from the server. A retraining trains on all of them; the engine skips the ones a version already contains."""
    path = package / "data" / "learned.jsonl"
    texts = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if extra_file:
        texts += extra_file.read_text(encoding="utf-8").splitlines()
    out: dict[str, list[str]] = {}
    for line in texts:
        try:
            row = json.loads(line)
            entry, question = row["entry_id"], " ".join(str(row["question"]).split())
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(entry, str) and question and question.lower() not in {q.lower() for q in out.setdefault(entry, [])}:
            out[entry].append(question)
    return {k: v for k, v in out.items() if v}


def _score(engine, ho, kb) -> dict:
    """Hit@1 and MRR@10 of an engine as it is deployed, on the held-out test questions (the in-scope ones)."""
    idx = ho.where("test", IN_SCOPE_TYPES)
    col = {e["id"]: j for j, e in enumerate(engine.entries)}
    gold = [frozenset(col[kb.entries[g].id] for g in ho.gold[i]) for i in idx]
    pq = metrics.per_query_sets(engine.scores([ho.queries[i] for i in idx]), gold)
    return {"n": len(idx), "hit@1": float(pq["hit@1"].mean()), "mrr@10": float(pq["mrr@10"].mean())}


def refresh_web(package: Path, dest: Path, notes: list[str]) -> dict[str, Path]:
    """Read the package's web pages (source/web.json) again into `dest`. A page that cannot be read now keeps its old copy, with a note.
    Returns the refreshed files by name."""
    from . import web

    listing = package / "source" / "web.json"
    out: dict[str, Path] = {}
    for item in json.loads(listing.read_text()) if listing.is_file() else []:
        try:
            page = web.read_page(item["url"])
        except web.WebError as e:
            notes.append(f"{item['url']}: {e}; the earlier copy is kept")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        (dest / item["file"]).write_bytes(web.page_html(page))
        out[item["file"]] = dest / item["file"]
    return out


def improve_package(package: str | Path, promote: bool = True, device: str = "auto", extra_file: str | Path | None = None,
                    on_event: Callable[[dict], None] | None = None, refresh_pages: bool = False) -> dict:
    """Build a candidate and decide what to do with it. Returns a report; `outcome` is one of
    promoted (it is live now), staged (built, but there is nothing to judge it by, so a person must promote it), kept_current (it was worse)."""
    from .runtime.engine import Engine

    pkg = Path(package).resolve()
    emit = on_event or (lambda e: None)
    files = sorted(p for p in (pkg / "source" / "files").glob("*") if p.is_file())
    if not files:
        raise ValueError("this package has no source/files to retrain from (it was exported by an older version of the tool)")
    regression = pkg / "source" / "regression.csv"  # the deployment regression suite: release checks. It influences which version goes live, so it is NOT a test set.
    regression = regression if regression.is_file() else None  # (source/heldout.csv, the locked benchmark, is for reporting and is never read here)
    extra = learned_questions(pkg, Path(extra_file) if extra_file else None)
    label = versions.stamp()
    work = pkg / "versions" / f".build-{label}"
    shutil.rmtree(work, ignore_errors=True)
    web_notes: list[str] = []
    fresh = refresh_web(pkg, work / "sources", web_notes) if refresh_pages else {}
    files = [fresh.get(p.name, p) for p in files]  # web pages read again today replace their old copies in this build
    run_auto(files, work / "run", emit, device=device, extra=extra)
    listing = pkg / "source" / "web.json"
    export_run(work / "run", work / "package" / pkg.name, pkg.name, sources=files, regression=regression, heldout=(pkg / "source" / "heldout.csv") if (pkg / "source" / "heldout.csv").is_file() else None,
               web=json.loads(listing.read_text()) if listing.is_file() else None)
    candidate = work / "package" / pkg.name

    report = {"label": label, "learned_questions": sum(map(len, extra.values())), "regression_questions": 0, "live": None, "candidate": None,
              "outcome": "staged", "reason": "", "web_pages_read_again": len(fresh), "web_notes": web_notes}
    current, new = Engine(pkg), Engine(candidate)
    kb = load_kb(work / "run" / "kb.json")
    same = {e["id"] for e in current.entries} == {e["id"] for e in new.entries}
    report["gate_set"] = "deployment regression suite (source/regression.csv): it influences promotion, so it is not a test set"
    if regression is None:
        report["reason"] = "there is no regression suite (source/regression.csv) to check it against"
    elif not same:
        report["reason"] = "the entries changed since the live version, so the two cannot be scored on the same questions"
    else:
        ho = load_heldout(regression, kb)  # questions that are near-duplicates of anything it was trained on, learned questions included, leave the suite
        n = len(ho.where("test", IN_SCOPE_TYPES))
        report["regression_questions"] = n
        if n < MIN_TEST:
            report["reason"] = f"only {n} regression questions remain after removing near-duplicates of training phrasings ({MIN_TEST} are needed)"
        else:
            live, cand = _score(current, ho, kb), _score(new, ho, kb)
            report.update(live=live, candidate=cand)
            if cand["hit@1"] >= live["hit@1"] and cand["mrr@10"] >= live["mrr@10"]:
                report.update(outcome="promoted" if promote else "staged",
                              reason=f"right entry first {live['hit@1']:.1%} → {cand['hit@1']:.1%} on {n} regression questions, and not worse on ranking")
            else:
                report.update(outcome="kept_current", reason=f"the candidate is worse on {n} regression questions (right entry first {live['hit@1']:.1%} → {cand['hit@1']:.1%}, "
                                                              f"MRR@10 {live['mrr@10']:.3f} → {cand['mrr@10']:.3f}), so the live version stays")

    (pkg / "versions" / "reports").mkdir(parents=True, exist_ok=True)
    if report["outcome"] == "promoted":
        report["previous_saved_as"] = versions.promote(pkg, candidate, label)
    elif report["outcome"] == "staged":
        staged = pkg / "versions" / f"staged-{label}"
        shutil.move(str(candidate), staged)
        report["staged_at"] = str(staged)
    (pkg / "versions" / "reports" / f"{label}.json").write_text(json.dumps(report, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    return report
