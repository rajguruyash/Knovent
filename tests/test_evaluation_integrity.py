"""The held-out questions are only ever measured against. Slow: it trains twice (FAQENGINE_SLOW=1)."""
import os
from pathlib import Path

import numpy as np
import pytest

from faqengine.heldout import load_heldout
from faqengine.kb import load_kb

ROOT = Path(__file__).resolve().parents[1]
BIKE = ROOT / "src" / "faqengine" / "studio" / "static" / "example-bike-shop.csv"


def test_a_split_column_cannot_make_a_held_out_question_a_tuning_question(tmp_path):
    kb = load_kb(BIKE)
    path = tmp_path / "t.csv"
    path.write_text("query,entry_id,type,split\nwhat time do you shut on a saturday,hours,paraphrase,dev\ncan i send back something i bought,returns,paraphrase,test\n")
    ho = load_heldout(path, kb)
    assert ho.split == ["test", "test"] and any("only ever used to measure" in w for w in ho.warnings)  # every row of the file is a test row


def test_the_hybrid_blend_is_chosen_on_validation_and_never_on_the_test_questions():
    from faqengine.heldout import HeldOut
    from faqengine.heldout_report import ALPHAS, hybrid_experiment, hybrid_scores

    rng = np.random.default_rng(0)
    n_entries = 6
    ho = HeldOut([], [], [], [], [])
    dense, keyword = [], []
    for split, keyword_is_better in (("dev", True), ("test", False)):  # on validation the keywords are the reliable signal, on the test questions the model is
        for i in range(24):
            gold = i % n_entries
            good, noisy = np.full(n_entries, 0.1), rng.uniform(0, 1, n_entries)
            good[gold] = 1.0
            ho.queries.append(f"{split} {i}"), ho.gold.append(frozenset({gold})), ho.types.append("paraphrase"), ho.split.append(split), ho.source.append("")
            keyword.append(good if keyword_is_better else noisy), dense.append(noisy if keyword_is_better else good)
    mats = {"bm25": np.array(keyword), "base": np.array(dense), "tuned": np.array(dense)}
    out = hybrid_experiment(mats, ho, "tuned")
    assert out["alpha"] == max(ALPHAS) or out["alpha"] >= 0.7  # chosen where the validation questions said the keywords win...
    assert out["overall"]["embedding"]["hit@1"] > out["overall"]["hybrid"]["hit@1"]  # ...even though that is worse on the test questions: the test did not steer it
    assert set(out["overall"]) == {"keyword", "embedding", "hybrid"} and "Experiment only" in out["verdict"]
    assert np.allclose(hybrid_scores(mats["tuned"], mats["bm25"], 0.0).argmax(1), mats["tuned"].argmax(1))  # alpha 0 is the model alone


@pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")
def test_nothing_the_engine_is_made_from_depends_on_the_held_out_file(tmp_path):
    """Train twice with the same seed and two entirely different held-out files. If any held-out question influenced the index, the vocabulary, training,
    model selection or a threshold, something saved would differ."""
    from faqengine.auto import run_auto

    marker = "zxqvblorf"
    one = tmp_path / "one.csv"
    one.write_text((ROOT / "examples" / "bike-shop" / "heldout.csv").read_text())
    two = tmp_path / "two.csv"
    two.write_text(f"query,entry_id,type\n{marker} sunday opening,hours,paraphrase\ntell me about {marker} refunds,returns,paraphrase\nwho won the {marker} match,,out-of-scope-far\n")
    runs = {}
    for name, held in (("one", one), ("two", two)):
        run_auto(BIKE, tmp_path / name, device="cpu", heldout_path=held)
        runs[name] = tmp_path / name
    a, b = runs["one"], runs["two"]
    assert (a / "kb.json").read_text() == (b / "kb.json").read_text()  # the searchable entries
    assert marker not in (b / "kb.json").read_text() and marker not in "".join(p.read_text() for p in (b / "model").glob("*.json") if p.stat().st_size < 5_000_000)  # not in the index, the tokenizer or the config
    assert np.allclose(np.load(a / "views.npy"), np.load(b / "views.npy"), atol=1e-5)  # the vectors of the index
    import json

    ta, tb = json.loads((a / "thresholds.json").read_text()), json.loads((b / "thresholds.json").read_text())
    assert ta["floor"] == pytest.approx(tb["floor"], abs=1e-6)  # the refusal line was not calibrated on the held-out questions
    la, lb = json.loads((a / "train_log.json").read_text()), json.loads((b / "train_log.json").read_text())
    assert la["steps"] == lb["steps"] and la["best_step"] == lb["best_step"] and la["beat_untuned_on_dev"] == lb["beat_untuned_on_dev"]  # nor training or model selection
    assert np.allclose([c["train_loss"] for c in la["curve"]], [c["train_loss"] for c in lb["curve"]], atol=1e-5)
    from safetensors.numpy import load_file

    wa, wb = load_file(str(next((a / "model").glob("*.safetensors")))), load_file(str(next((b / "model").glob("*.safetensors"))))
    assert all(np.allclose(wa[k], wb[k], atol=1e-6) for k in wa)  # the weights themselves
    assert json.loads((a / "report.json").read_text())["heldout"]["test"]["n"] != json.loads((b / "report.json").read_text())["heldout"]["test"]["n"]  # while what is REPORTED does depend on the file
