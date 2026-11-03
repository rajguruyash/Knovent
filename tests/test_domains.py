"""FAQs from unrelated fields and languages go through the whole pipeline without falling apart (examples/domains, built by
scripts/build_domain_examples.py; scripts/domain_sweep.py runs all of them and tabulates the result).

Slow (trains models), so it only runs with FAQENGINE_SLOW=1.
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")

DOMAINS = Path(__file__).resolve().parents[1] / "examples" / "domains"


@pytest.mark.parametrize("name", ["clinic", "saas", "restaurant"])
def test_a_field_with_typed_held_out_questions_is_measured_not_assumed(name, tmp_path):
    from faqengine.auto import run_auto

    report = run_auto(DOMAINS / name / "faq.csv", tmp_path / "run", device="cpu", heldout_path=DOMAINS / name / "heldout.csv")
    h = report["heldout"]
    ship = h["decision"]["ship"]
    shipped, keyword = h["test"]["overall"][ship]["hit@1"], h["test"]["overall"]["bm25"]["hit@1"]
    assert report["mode"] == "heldout" and h["test"]["n"] >= 10
    assert shipped >= 0.6 and shipped >= keyword - 0.1  # nothing about the subject makes the engine fall apart, and it is not worse than keyword search
    assert h["refusal"][ship]["test"]["auroc_far"] >= 0.9  # questions about something else entirely score lower than real ones, so a line can tell them apart (how well it is placed is reported, not asserted: a handful of questions)


def test_a_faq_whose_subjects_overlap_the_generic_off_topic_questions_does_not_misreport_refusal(tmp_path):
    from faqengine.auto import run_auto

    report = run_auto(DOMAINS / "trivia" / "faq.csv", tmp_path / "run", device="cpu")  # no held-out file: the generated checks
    left_out = report["selfcheck"]["probes_left_out"]
    assert "what is the capital of france" in left_out and "who wrote romeo and juliet" in left_out  # it answers those, so they cannot test refusal
    assert any("generic off-topic questions were not used" in v for v in report["verdict"])
    assert report["selfcheck"]["systems"]["tuned"]["hit@1"] > 0.5


@pytest.mark.parametrize("name,said", [("spanish-hotel", "Spanish"), ("hinglish-store", "Hinglish")])
def test_text_in_another_language_is_flagged_and_still_works(name, said, tmp_path):
    from faqengine.auto import run_auto

    report = run_auto(DOMAINS / name / "faq.csv", tmp_path / "run", device="cpu")  # the default, English-only model
    assert any("looks like" in v and said in v and "multilingual" in v for v in report["verdict"])  # said up front, in the first lines of the report
    assert report["selfcheck"]["systems"]["tuned"]["hit@1"] > 0.4  # weaker than in English, but not broken
