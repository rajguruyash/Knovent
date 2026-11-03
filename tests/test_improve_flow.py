"""A deployed package retrains itself from its own source files and what it learned, and promotes the result only when it is not worse.

Slow (trains a model and exports ONNX twice), so it only runs with FAQENGINE_SLOW=1.
"""
import json
import os
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")

BIKE = Path(__file__).resolve().parents[1] / "examples" / "bike-shop"
STUDIO = Path(__file__).resolve().parents[1] / "src" / "faqengine" / "studio" / "static" / "example-bike-shop.csv"


@pytest.fixture(scope="module")
def deployed(tmp_path_factory):
    """A package made the way the studio makes one: trained on the FAQ with held-out questions, with its sources inside."""
    from faqengine.auto import export_run, run_auto

    tmp = tmp_path_factory.mktemp("improve")
    run_auto(STUDIO, tmp / "run", device="cpu", heldout_path=BIKE / "heldout.csv")
    export_run(tmp / "run", tmp / "bike-shop", "bike-shop", sources=[STUDIO], heldout=BIKE / "heldout.csv", regression=BIKE / "regression.csv")
    return tmp / "bike-shop"


def test_the_package_carries_what_it_needs_to_retrain_itself(deployed):
    assert (deployed / "source" / "files" / "example-bike-shop.csv").is_file() and (deployed / "source" / "heldout.csv").is_file() and (deployed / "source" / "regression.csv").is_file()
    assert (deployed / "ci" / "improve.py").is_file() and (deployed / "ci" / "github-actions.yml").is_file() and (deployed / "ci" / "README.md").is_file()
    assert "Keep it improving in production" in (deployed / "README.md").read_text()


def test_retraining_learns_from_taught_questions_and_promotes_only_if_not_worse(deployed, tmp_path):
    from faqengine.improve import improve_package
    from faqengine.runtime import versions

    pkg = tmp_path / "live"
    shutil.copytree(deployed, pkg)
    import importlib.util

    spec = importlib.util.spec_from_file_location("live_engine", pkg / "runtime" / "engine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.Engine(pkg).learn("delivery", "will a courier bring the bike round to my flat")

    report = improve_package(pkg, device="cpu")
    assert report["learned_questions"] == 1 and report["regression_questions"] >= 20
    assert report["outcome"] in ("promoted", "kept_current"), report  # with held-out questions it is always judged, never just staged
    assert report["live"]["n"] == report["candidate"]["n"] == report["regression_questions"]
    assert (pkg / "versions" / "reports" / f"{report['label']}.json").is_file() and not list((pkg / "versions").glob(".build-*"))
    taught = "will a courier bring the bike round to my flat"
    entries = json.loads((pkg / "kb" / "entries.json").read_text())["entries"]
    delivery = next(e for e in entries if e["id"] == "delivery")
    if report["outcome"] == "promoted":
        assert report["candidate"]["hit@1"] >= report["live"]["hit@1"] and report["candidate"]["mrr@10"] >= report["live"]["mrr@10"]
        assert taught in delivery["questions"]  # the taught question is now part of what the model was trained on and indexed with
        assert versions.saved(pkg) and (pkg / "data" / "learned.jsonl").is_file()
        versions.rollback(pkg)  # and the old version comes back, with the taught question still searched live
        assert taught not in next(e for e in json.loads((pkg / "kb" / "entries.json").read_text())["entries"] if e["id"] == "delivery")["questions"]
        assert mod.Engine(pkg).retrieve(taught, 1)[0]["matched"] == taught
    else:
        assert taught not in delivery["questions"] and not versions.saved(pkg)  # nothing changed


def test_without_held_out_questions_the_candidate_is_staged_not_promoted(deployed, tmp_path):
    from faqengine.improve import improve_package
    from faqengine.runtime import versions

    pkg = tmp_path / "live"
    shutil.copytree(deployed, pkg)
    (pkg / "source" / "regression.csv").unlink()
    report = improve_package(pkg, device="cpu")
    assert report["outcome"] == "staged" and "no regression suite" in report["reason"] and Path(report["staged_at"]).is_dir()
    assert not versions.saved(pkg) and (pkg / "kb" / "entries.json").is_file()  # the live version was not touched
    versions.promote(pkg, report["staged_at"])  # a person looked at it and decided
    assert len(versions.saved(pkg)) == 1


def test_promotion_is_decided_on_the_regression_suite_and_never_reads_the_locked_benchmark(deployed, tmp_path):
    from faqengine.improve import improve_package

    pkg = tmp_path / "live"
    shutil.copytree(deployed, pkg)
    (pkg / "source" / "heldout.csv").write_text("this is not even a csv of questions\n\x00")  # if the gate read the benchmark it would fail on this
    report = improve_package(pkg, device="cpu")
    assert report["outcome"] in ("promoted", "kept_current") and "regression suite" in report["gate_set"] and "not a test set" in report["gate_set"]
    assert (pkg / "source" / "heldout.csv").read_text().startswith("this is not")  # and it was carried over untouched, for reporting
