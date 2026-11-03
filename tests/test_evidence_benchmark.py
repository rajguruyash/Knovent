"""The evidence sets: the locked benchmark stays locked, the regression suite is a release check, dev is the floor for development.

The first two tests are fast. The last runs the benchmark with a real model (FAQENGINE_SLOW=1).
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "examples" / "evidence"
BENCH = ROOT / "scripts" / "evidence_benchmark.py"


def test_the_locked_benchmark_has_not_been_edited_and_cannot_be_read_question_by_question():
    assert hashlib.sha1((EVID / "locked.csv").read_bytes()).hexdigest() == (EVID / "locked.sha1").read_text().strip()  # change it and the numbers are no longer comparable
    out = subprocess.run([sys.executable, str(BENCH), "--set", "locked", "--detail"], capture_output=True, text=True, timeout=60)
    assert out.returncode != 0 and "aggregate numbers only" in (out.stdout + out.stderr)


def test_the_three_sets_are_disjoint_and_cover_every_category():
    import csv

    sets = {n: list(csv.DictReader((EVID / f"{n}.csv").open(encoding="utf-8"))) for n in ("dev", "regression", "locked")}
    ids = {n: {r["id"] for r in rows} for n, rows in sets.items()}
    assert not (ids["dev"] & ids["regression"] or ids["dev"] & ids["locked"] or ids["regression"] & ids["locked"])
    questions = {n: {r["question"].lower() for r in rows} for n, rows in sets.items()}
    assert not (questions["dev"] & questions["locked"] or questions["regression"] & questions["locked"] or questions["dev"] & questions["regression"])  # no question is in two sets
    categories = {r["category"] for rows in sets.values() for r in rows}
    assert {"direct", "paraphrase", "partial", "related", "unsupported", "multi-part", "conflict"} <= categories  # every kind asked for is in the benchmark
    assert {r["category"] for r in sets["locked"]} == categories


@pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")
def test_the_regression_suite_passes_and_dev_does_not_regress(tmp_path):
    results = {}
    for name in ("regression", "dev"):
        out = tmp_path / f"{name}.json"
        run = subprocess.run([sys.executable, str(BENCH), "--set", name, "--impl", "after", "--run", str(tmp_path / "kb"), "--json", str(out)], capture_output=True, text=True, timeout=900)
        assert run.returncode == 0, run.stderr[-600:]
        results[name] = json.loads(out.read_text())
    reg, dev = results["regression"], results["dev"]
    assert reg["correct_answer_rate"] == 1.0 and reg["false_answer_rate"] == 0.0 and reg["abstention_recall"] == 1.0  # a release must keep all of these
    assert dev["correct_answer_rate"] >= 0.8 and dev["false_answer_rate"] == 0.0 and dev["abstention_recall"] == 1.0 and dev["conflict_detection"] == 1.0
    assert dev["multi_part_completion"] >= 0.5 and dev["partial_handling"] == 1.0
