"""The cross-encoder experiment is isolated: the bookkeeping is right, the production code does not know about it, and the questions are the same for every configuration."""
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.cross_encoder.rerank import MODELS, TOP, metrics_for, order, rerank  # noqa: E402


def test_reranking_reorders_only_the_candidates_and_records_every_rank_and_score():
    base = np.array([0.1, 0.9, 0.5, 0.8, 0.3] + [0.0] * 30)  # retrieval order: 1, 3, 2, 4, 0, then the rest
    ce = np.linspace(0, 1, TOP)  # the reranker likes the retrieval's last candidate best of all
    out = rerank(base, ce)
    assert len(out["candidates"]) == TOP and out["final"][0] == order(base)[TOP - 1]  # the 20th candidate is now first
    assert list(out["final"][TOP:]) == list(order(base)[TOP:])  # below the top 20 nothing moves
    first = out["candidates"][0]
    assert first["entry"] == 1 and first["original_rank"] == 1 and first["original_score"] == 0.9 and first["reranker_score"] == ce[0] and first["final_rank"] == TOP  # all four recorded
    assert sorted(c["final_rank"] for c in out["candidates"]) == list(range(1, TOP + 1))
    assert (rerank(base, ce)["final"] == out["final"]).all()  # reproducible


def test_the_metrics_count_what_they_say():
    ranking = np.array([5, 2, 9, 1, 0, 3, 4, 6, 7, 8] + list(range(10, 30)))
    one = metrics_for(ranking, frozenset({9}))
    assert one["right_first"] == 0 and one["mrr@10"] == pytest.approx(1 / 3) and one["recall@5"] == 1.0 and one["best_gold_rank"] == 3 and one["gold_in_top20"] == 1.0
    two = metrics_for(ranking, frozenset({5, 25}))
    assert two["right_first"] == 1.0 and two["recall@5"] == 0.5 and two["recall@10"] == 0.5 and two["recall@20"] == 0.5 and two["gold_in_top20"] == 0.0  # one of the two is below the top 20
    assert metrics_for(ranking, frozenset({5}))["ndcg@10"] == 1.0 and metrics_for(ranking, frozenset({29}))["mrr@10"] == 0.0


def test_the_production_code_knows_nothing_about_the_experiment():
    for path in (ROOT / "src").rglob("*.py"):
        assert not re.search(r"\bexperiments\b|cross_encoder|CrossEncoder", path.read_text()), path  # nothing imports it, so nothing about the engine changed
    assert "experiments" not in (ROOT / "pyproject.toml").read_text()  # and it is not part of the installed package


def test_every_configuration_is_asked_the_same_questions_and_the_pools_do_not_overlap():
    from experiments.cross_encoder.datasets import build

    data = build()
    for ds in data:
        assert len(ds.queries) == len(ds.gold) == len(ds.category) == len(ds.pool) and all(g for g in ds.gold)  # every question has right entries
        assert len({q.lower() for q, p in zip(ds.queries, ds.pool)}) == len(ds.queries)  # and appears once, in one pool
    locked = [ds for ds in data if "locked" in ds.pool]
    assert [d.name for d in locked] == ["evidence"] and all(p in ("dev", "locked") for p in locked[0].pool)
    assert not any(p == "locked" for ds in data if ds.name != "evidence" for p in ds.pool)  # only the locked evidence set feeds the locked pool


@pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")
def test_a_real_cross_encoder_scores_offline_and_prefers_the_relevant_text():
    from experiments.cross_encoder.rerank import CrossEncoder

    old = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        try:
            ce = CrossEncoder(MODELS["minilm-l6"], "cpu")
        except OSError:
            pytest.skip("the cross-encoder has not been downloaded on this machine")
        s = ce.score("how long does a grooming session take", ["A standard grooming appointment usually takes 60 to 90 minutes.", "We accept UPI, cash and cards.", ""])
        assert np.isfinite(s).all() and s[0] > s[1]  # the relevant text outranks the irrelevant one, with no network
    finally:
        if old is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old
