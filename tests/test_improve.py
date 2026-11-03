"""Promoting a new version of a deployed package, rolling it back, and what a deployment has learned. No model needed."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from faqengine.runtime import versions

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "faqengine" / "ci" / "improve.py"


def make(root: Path, tag: str) -> None:
    """The parts of one version of a package, each holding `tag`."""
    for part in ("model", "kb", "eval"):
        (root / part).mkdir(parents=True, exist_ok=True)
        (root / part / "file.txt").write_text(tag)
    (root / "thresholds.json").write_text(tag)
    (root / "README.md").write_text(tag)
    (root / "source").mkdir(exist_ok=True)
    (root / "source" / "files.txt").write_text(tag)  # the files a version was trained from travel with it


def tag(root: Path) -> str:
    return (root / "kb" / "file.txt").read_text()


def test_promote_swaps_every_part_keeps_the_old_version_and_rollback_undoes_it(tmp_path):
    pkg, cand = tmp_path / "pkg", tmp_path / "cand"
    make(pkg, "v1")
    (pkg / "runtime").mkdir()
    (pkg / "runtime" / "engine.py").write_text("x")
    make(cand, "v2")
    old = versions.promote(pkg, cand, "20260101-000000")
    assert tag(pkg) == "v2" and (pkg / "thresholds.json").read_text() == "v2" and (pkg / "source" / "files.txt").read_text() == "v2"
    assert tag(pkg / "versions" / old) == "v1" and (pkg / "versions" / old / "source" / "files.txt").read_text() == "v1" and (pkg / "runtime" / "engine.py").exists()  # only the parts of a version move
    assert versions.saved(pkg) == [pkg / "versions" / old]
    versions.rollback(pkg)
    assert tag(pkg) == "v1" and (pkg / "thresholds.json").read_text() == "v1" and (pkg / "source" / "files.txt").read_text() == "v1"
    versions.rollback(pkg)  # the version that was left is saved too, so rolling back twice undoes the first rollback
    assert tag(pkg) == "v2"
    with pytest.raises(ValueError, match="no earlier version"):
        versions.rollback(tmp_path / "empty")


def test_a_swap_that_fails_part_way_is_undone(tmp_path, monkeypatch):
    pkg, cand = tmp_path / "pkg", tmp_path / "cand"
    make(pkg, "v1")
    cand.mkdir()
    (cand / "kb").mkdir()
    with pytest.raises(ValueError, match="not a version"):
        versions.promote(pkg, cand)  # no model/: not a version at all
    assert tag(pkg) == "v1" and not (pkg / "versions").exists()

    make(cand, "v2")
    real, calls = versions.os.replace, []

    def flaky(a, b):
        calls.append(a)
        if len(calls) == 4:
            raise OSError("disk full")
        return real(a, b)

    monkeypatch.setattr(versions.os, "replace", flaky)
    with pytest.raises(OSError, match="disk full"):
        versions.promote(pkg, cand, "x")
    monkeypatch.undo()
    assert tag(pkg) == "v1" and (pkg / "thresholds.json").read_text() == "v1" and tag(cand) == "v2" and not (pkg / "versions" / "x").exists()


def test_old_versions_are_pruned_and_staged_ones_are_never_rolled_back_to(tmp_path):
    pkg = tmp_path / "pkg"
    make(pkg, "v0")
    for i in range(1, versions.KEEP + 3):
        cand = tmp_path / f"c{i}"
        make(cand, f"v{i}")
        versions.promote(pkg, cand, f"2026010{i}-000000")
    assert len(versions.saved(pkg)) == versions.KEEP
    make(pkg / "versions" / "staged-x", "never live")
    assert all("staged" not in d.name for d in versions.saved(pkg))
    versions.rollback(pkg)
    assert tag(pkg) == f"v{versions.KEEP + 1}"  # the newest version that was live, not the staged candidate


def test_learned_questions_are_gathered_deduplicated_and_survive_damage(tmp_path):
    from faqengine.improve import learned_questions

    (tmp_path / "data").mkdir()
    rows = [json.dumps({"entry_id": "a", "question": "How  do I pay?"}), json.dumps({"entry_id": "a", "question": "how do i pay?"}), "not json",
            json.dumps({"entry_id": "b", "question": "x"}), '{"entry_id": "b"']  # the last line is cut off, as after a crash
    (tmp_path / "data" / "learned.jsonl").write_text("\n".join(rows))
    prod = tmp_path / "prod.jsonl"
    prod.write_text(json.dumps({"entry_id": "c", "question": "y"}) + "\n" + json.dumps({"entry_id": "b", "question": "X"}) + "\n")
    assert learned_questions(tmp_path, prod) == {"a": ["How do I pay?"], "b": ["x"], "c": ["y"]}
    assert learned_questions(tmp_path / "nothing") == {}


def test_the_ci_script_reports_and_forgets_learned_questions(tmp_path):
    (tmp_path / "data").mkdir()
    rows = [{"entry_id": "a", "question": "How do I pay?"}, {"entry_id": "b", "question": "can I post it back"}]
    (tmp_path / "data" / "learned.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def run(*args):
        out = subprocess.run([sys.executable, str(SCRIPT), "--package", str(tmp_path), *args], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr[-400:]
        return out.stdout

    assert "2 questions" in run("status") and "none: a retrained candidate will be staged" in run("status")
    assert "forgot 1" in run("forget", "a", "HOW do i   pay?")  # case and spacing do not matter
    assert "1 questions" in run("status")
    assert json.loads((tmp_path / "data" / "learned.jsonl").read_text())["entry_id"] == "b"
