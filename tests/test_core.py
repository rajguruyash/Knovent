"""Tests for the parts that do not need a model: data loading, BM25, augmentation, metrics, calibration."""
import json
import random

import numpy as np
import pytest

from faqengine import metrics
from faqengine.augment import augment
from faqengine.bm25 import BM25, bm25_entry_scores, tokenize
from faqengine.evaluate import bands, calibrate, fuse, pick_weight, wilson_lower
from faqengine.kb import KB, Entry, load_kb, load_queries
from faqengine.train import make_batches


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_csv_with_alternates_and_generated_ids(tmp_path):
    p = write(tmp_path, "f.csv", "question,answer,alt_questions\nHow do I reset?,Click reset.,reset password | forgot password\nWhere are you?,Here.,\n")
    kb = load_kb(p)
    assert [e.id for e in kb.entries] == ["faq-001", "faq-002"]
    assert kb.entries[0].questions == ["How do I reset?", "reset password", "forgot password"]
    assert [v.kind for v in kb.views] == ["q", "q", "q", "qa", "q", "qa"]


def test_duplicate_ids_are_rejected_and_duplicate_questions_warned(tmp_path):
    with pytest.raises(ValueError, match="duplicate id"):
        load_kb(write(tmp_path, "a.csv", "id,question,answer\nx,Q1,A\nx,Q2,A\n"))
    kb = load_kb(write(tmp_path, "b.csv", "question,answer\nSame?,A\nsame?,B\n"))
    assert any("same question" in w for w in kb.warnings)


def test_json_and_jsonl_formats(tmp_path):
    rows = [{"question": "Q1", "answer": "A1"}, {"questions": ["Q2", "Q2b"], "answer": "A2", "id": "two"}]
    kb = load_kb(write(tmp_path, "a.json", json.dumps({"faqs": rows})))
    assert kb.entries[1].id == "two" and kb.entries[1].questions == ["Q2", "Q2b"]
    kb = load_kb(write(tmp_path, "b.jsonl", "\n".join(json.dumps(r) for r in rows)))
    assert len(kb) == 2


def test_queries_mark_out_of_scope(tmp_path):
    kb = KB([Entry("a", ["Q"], "A"), Entry("b", ["R"], "B")])
    q = load_queries(write(tmp_path, "q.csv", "query,entry_id\nfoo,b\nbar,\nbaz,-\n"), kb)
    assert q.gold.tolist() == [1, -1, -1]
    with pytest.raises(ValueError, match="unknown entry_id"):
        load_queries(write(tmp_path, "q2.csv", "query,entry_id\nfoo,nope\n"), kb)


def test_entry_max_scores_an_entry_by_its_best_view():
    kb = KB([Entry("a", ["q1", "q2"], "ans"), Entry("b", ["q3"], "")])  # views: a:q1 a:q2 a:qa b:q3
    s = np.array([[0.1, 0.9, 0.3, 0.5]])
    assert kb.entry_max(s).tolist() == [[0.9, 0.5]]
    assert [v.text for v in kb.views_of(1)] == ["q3"]


def test_bm25_prefers_the_matching_document_and_ignores_stopwords():
    docs = ["reset your password in settings", "opening hours are nine to five", "delivery takes two days"]
    scores = BM25(docs).scores("how do I reset my password?")
    assert scores.argmax() == 0 and scores[1] == 0 and scores[2] == 0
    assert tokenize("the Passwords are being reset") == tokenize("password reset")


def test_bm25_entry_scores_take_the_best_view():
    kb = KB([Entry("a", ["opening hours"], ""), Entry("b", ["delivery cost", "shipping price"], "")])
    s = bm25_entry_scores(kb, ["shipping price please"])
    assert s.argmax() == 1


def test_augment_gives_distinct_variants_and_is_deterministic():
    q = "How long does a repair usually take for my bike?"
    a = augment(q, random.Random(1), 4)
    assert len(a) == len(set(a)) == 4 and q not in a
    assert a == augment(q, random.Random(1), 4)
    assert augment("Hi", random.Random(1), 4) == [] or all(x != "Hi" for x in augment("Hi", random.Random(1), 4))


def test_batches_never_repeat_an_entry():
    rng = random.Random(0)
    examples = [(f"q{i}", i % 7) for i in range(100)]
    batches = make_batches(examples, 5, rng)
    assert batches and all(len({examples[i][1] for i in b}) == len(b) <= 5 for b in batches)
    assert len({i for b in batches for i in b}) == sum(len(b) for b in batches)  # no example used twice


def test_ranks_count_ties_against_the_system():
    scores = np.array([[0.0, 0.0, 0.0], [0.5, 0.9, 0.1]])
    gold = np.array([1, 0])
    assert metrics.ranks(scores, gold).tolist() == [3, 2]  # all-zero row: the right entry ranks last


def test_metric_values():
    scores = np.array([[0.9, 0.5, 0.1], [0.2, 0.9, 0.5], [0.1, 0.2, 0.9], [0.5, 0.5, 0.5]])
    gold = np.array([0, 2, 2, -1])  # rank 1, rank 2, rank 1, out of scope (ignored)
    m = metrics.summarize(scores, gold)
    assert m["hit@1"] == pytest.approx(2 / 3)
    assert m["hit@3"] == 1.0 and m["p@3"] == pytest.approx(1 / 3)
    assert m["mrr@10"] == pytest.approx((1 + 0.5 + 1) / 3)
    assert m["ndcg@10"] == pytest.approx((1 + 1 / np.log2(3) + 1) / 3)


def test_listwise_loss_is_low_when_the_right_entry_wins():
    good = np.array([[0.9, 0.1], [0.1, 0.9]])
    assert metrics.listwise_loss(good, np.array([0, 1]), 20) < 0.01
    assert metrics.listwise_loss(good, np.array([1, 0]), 20) > 10


def test_auroc_and_intervals():
    assert metrics.auroc(np.array([0.9, 0.8]), np.array([0.1, 0.2])) == 1.0
    assert metrics.auroc(np.array([0.5]), np.array([0.5])) == 0.5
    mean, half = metrics.mean_ci([1.0, 2.0, 3.0])
    assert mean == 2.0 and half == pytest.approx(4.30 * 1 / np.sqrt(3))
    d, lo, hi = metrics.paired_bootstrap(np.zeros(200), np.ones(200))
    assert d == lo == hi == 1.0


def test_fusion_weight_zero_is_the_dense_score_and_pick_weight_can_choose_it():
    dense = np.array([[0.9, 0.1], [0.2, 0.8]])
    lexical = np.array([[0.0, 3.0], [4.0, 0.0]])
    assert np.array_equal(fuse(dense, lexical, 0.0), dense)
    assert pick_weight(dense, lexical, np.array([0, 1])) == 0.0  # dense alone is already right; zero wins the tie


def test_wilson_bound_is_below_the_observed_rate_and_tightens_with_more_data():
    small = wilson_lower(np.array([9.0]), np.array([10.0]))[0]
    large = wilson_lower(np.array([900.0]), np.array([1000.0]))[0]
    assert small < large < 0.9


def test_calibration_and_bands_on_separable_scores():
    rng = np.random.default_rng(0)
    n, e = 300, 5
    gold = rng.integers(0, e, n)
    S = rng.uniform(0.0, 0.4, (n, e))
    right = rng.random(n) < 0.8
    S[np.arange(n)[right], gold[right]] = rng.uniform(0.7, 1.0, right.sum())  # 80% are answered well
    th = calibrate(S, gold, target_precision=0.95)
    assert th["direct"] is not None and th["floor"] <= th["direct"]
    assert 0.3 < th["direct"] <= 0.7  # low enough to answer every well-matched question, high enough to skip most of the rest
    b = bands(S, gold, th)
    assert b["in_direct_share"] + b["in_llm_share"] + b["in_none_share"] == pytest.approx(1.0)
    assert b["in_direct_acc"] > 0.95
    never = calibrate(rng.uniform(0, 1, (n, e)), gold)  # scores carry no signal: direct answers are never safe
    assert never["direct"] is None


def test_contrastive_loss_never_punishes_a_view_of_the_right_entry():
    import torch

    from faqengine.train import contrastive_loss

    q = torch.nn.functional.normalize(torch.tensor([[1.0, 0.0], [0.0, 1.0]]), dim=-1)
    # docs: the right view of each query, then a hard negative for query 0 that is ANOTHER view of entry 1 (identical to query 1)
    d = torch.nn.functional.normalize(torch.tensor([[0.9, 0.1], [0.1, 0.9], [0.0, 1.0]]), dim=-1)
    masked = contrastive_loss(q, d, [0, 1], [0, 1, 1], scale=20.0)
    unmasked_if_wrong = contrastive_loss(q, d[:2], [0, 1], [0, 1], scale=20.0)
    assert torch.isfinite(masked)
    # entry 1's own view must not count against query 1: the loss with the extra same-entry doc equals the loss without it
    assert masked.item() == pytest.approx(unmasked_if_wrong.item(), abs=1e-3)
    # but a doc of ANOTHER entry in the same position does count against query 1
    hard = contrastive_loss(q, d, [0, 1], [0, 1, 0], scale=20.0)
    assert hard.item() > masked.item() + 1e-3


def test_set_metrics_match_the_single_entry_metrics_and_handle_several_right_answers():
    scores = np.array([[0.9, 0.5, 0.1], [0.2, 0.9, 0.5], [0.1, 0.2, 0.9]])
    single = metrics.per_query(scores, np.array([0, 2, 2]))
    sets = metrics.per_query_sets(scores, [frozenset({0}), frozenset({2}), frozenset({2})])
    for key in ("hit@1", "hit@3", "p@3", "mrr@10", "ndcg@10"):
        assert np.allclose(single[key], sets[key]), key
    # a question with two acceptable entries: they rank 1st and 3rd
    m = metrics.per_query_sets(np.array([[0.9, 0.5, 0.1]]), [frozenset({0, 2})])
    assert m["hit@1"][0] == 1 and m["p@3"][0] == pytest.approx(2 / 3) and m["r@1"][0] == 0.5 and m["r@3"][0] == 1.0
    assert m["mrr@10"][0] == 1.0 and m["ndcg@10"][0] == pytest.approx((1 + 1 / np.log2(4)) / (1 + 1 / np.log2(3)))
    # a tie is decided against the system
    tie = metrics.per_query_sets(np.array([[0.5, 0.5]]), [frozenset({1})])
    assert tie["hit@1"][0] == 0 and tie["mrr@10"][0] == 0.5


def test_multi_positive_loss_never_treats_another_positive_of_the_same_entry_as_a_negative():
    import torch

    from faqengine.train import multi_positive_loss

    q = torch.nn.functional.normalize(torch.tensor([[1.0, 0.0], [0.0, 1.0]]), dim=-1)
    # query 0 owns docs 0,1 (both near it); query 1 owns docs 2,3; doc 4 is a hard negative for query 0 belonging to entry 1
    near0 = torch.nn.functional.normalize(torch.tensor([[1.0, 0.1], [0.9, 0.0]]), dim=-1)
    near1 = torch.nn.functional.normalize(torch.tensor([[0.1, 1.0], [0.0, 0.9]]), dim=-1)
    hard = torch.nn.functional.normalize(torch.tensor([[0.6, 0.6]]), dim=-1)
    d = torch.cat([near0, near1, hard])
    loss = multi_positive_loss(q, d, [0, 1], [0, 0, 1, 1, 1], 2, 20.0)
    assert torch.isfinite(loss) and loss.item() < 0.05
    # moving the second positive of query 0 far away raises the loss (it is still a positive, so it must be pulled in)
    far = d.clone()
    far[1] = torch.nn.functional.normalize(torch.tensor([-1.0, 0.0]), dim=-1)
    assert multi_positive_loss(q, far, [0, 1], [0, 0, 1, 1, 1], 2, 20.0).item() > loss.item() + 1
    # a doc of entry 0 sitting right on query 1 is treated as a negative for query 1 and hurts
    bad = d.clone()
    bad[0] = torch.nn.functional.normalize(torch.tensor([0.0, 1.0]), dim=-1)
    assert multi_positive_loss(q, bad, [0, 1], [0, 0, 1, 1, 1], 2, 20.0).item() > loss.item()


def test_an_improvement_is_claimed_only_when_hit_at_1_clears_chance():
    from faqengine.heldout_report import decide, training_effect

    base = {"hit@1": np.r_[np.ones(88), np.zeros(12)], "mrr@10": np.r_[np.ones(88), np.full(12, 0.30)], "ndcg@10": np.r_[np.ones(88), np.full(12, 0.30)]}
    clear = {k: v.copy() for k, v in base.items()}
    clear["hit@1"][88:100] = 1.0  # twelve questions fixed, none broken: beyond chance
    eff = training_effect(base, clear)
    assert eff["demonstrated"] and decide(True, eff)["improvement_demonstrated"]

    # the right entry moves up the list but is not first more often: ranking is better, a Hit@1 gain is not claimed
    ranks = {k: v.copy() for k, v in base.items()}
    ranks["mrr@10"][88:100] = 0.5
    ranks["ndcg@10"][88:100] = 0.5
    eff = training_effect(base, ranks)
    assert eff["ranking_better"] and not eff["demonstrated"]
    d = decide(True, eff)
    assert d["ship"] == "tuned" and not d["improvement_demonstrated"] and "NOT claimed" in d["reason"]

    worse = {k: v.copy() for k, v in base.items()}
    worse["hit@1"][:5] = 0.0
    d = decide(True, training_effect(base, worse))
    assert d["ship"] == "tuned" and not d["improvement_demonstrated"] and "never used to choose" in d["reason"]  # test questions only decide what may be CLAIMED, not what ships
    assert decide(False, training_effect(base, clear))["ship"] == "base"  # the choice is the validation questions' alone


def test_generic_off_topic_questions_are_not_used_when_the_faq_covers_them():
    from faqengine.kb import Entry
    from faqengine.report import pick_floor
    from faqengine.selfcheck import OFF_TOPIC, usable_probes

    bike = [Entry("a", ["What are your opening hours?"], "We are open 9 to 6."), Entry("b", ["How do I return an item?"], "Within 30 days with the receipt.")]
    keep, left_out = usable_probes(bike)
    assert len(keep) >= 28 and len(keep) + len(left_out) == len(OFF_TOPIC)  # a bike shop answers almost none of them
    trivia = [Entry("c", ["What is the capital of France?"], "Paris."), Entry("d", ["How tall is Mount Everest?"], "8,849 metres."),
              Entry("e", ["Who wrote Romeo and Juliet?"], "William Shakespeare."), Entry("f", ["What is the weather like in London?"], "Often rainy.")]
    keep, left_out = usable_probes(trivia)
    assert "what is the capital of france" in left_out and "how tall is mount everest" in left_out and "who wrote romeo and juliet" in left_out
    assert "what is the weather like today" in left_out and "tell me a joke" in keep  # only what the FAQ really answers is left out
    # with nothing to measure refusal against, the line sits below a typical right match, not at zero
    right = np.array([0.8, 0.7, 0.9, 0.6, 0.75])
    assert 0.2 < pick_floor(np.array([]), right) <= 0.4 and pick_floor(np.array([]), np.array([])) == 0.0


def test_the_language_of_a_knowledge_base_is_noticed():
    from faqengine import language

    english = ["How do I reset my password?", "You can reset your password from the settings page, and we will send you an email with a link."] * 4
    spanish = ["¿Cuál es el horario de la tienda?", "La tienda abre de lunes a viernes de nueve a cinco y los sábados por la mañana."] * 4
    hindi = ["आपकी दुकान कब खुलती है?", "दुकान सुबह नौ बजे से शाम पाँच बजे तक खुलती है।"] * 4
    hinglish = ["Aapki dukaan kab khulti hai?", "Dukaan subah nau baje se shaam paanch baje tak khulti hai aur ravivaar ko band rehti hai."] * 4
    assert language.detect(english)["english"] and language.detect(english)["sure"]
    assert not language.detect(spanish)["english"] and language.detect(spanish)["language"] == "Spanish"
    assert not language.detect(hindi)["english"] and "Devanagari" in language.detect(hindi)["language"]
    assert not language.detect(hinglish)["english"] and "Hinglish" in language.detect(hinglish)["language"]
    unsure = language.detect(["SKU-1234", "A1", "B2"])
    assert unsure["english"] and not unsure["sure"]  # product codes: it does not pretend to know
    assert language.advice(language.detect(spanish), "en") and language.advice(language.detect(spanish), "multilingual") is None
    assert language.advice(language.detect(english), "en") is None and language.advice(unsure, "en") is None


def test_the_refusal_line_is_not_placed_by_a_couple_of_questions():
    from faqengine.heldout_report import choose_floor

    top_in = np.linspace(0.55, 0.9, 20)  # scores of the right matches
    cap = float(np.quantile(top_in, 0.10))
    # two out-of-scope dev questions of one's own: the line stays under the weakest tenth of the right matches, whatever they scored
    assert choose_floor(top_in, np.array([0.30, 0.35]), probe_tops=np.linspace(0.1, 0.3, 12)) <= cap
    assert choose_floor(top_in, np.array([0.95, 0.96])) <= cap  # a model that does not understand the language scores them high: it must not refuse the right matches wholesale
    # with enough out-of-scope questions of one's own the line is theirs to place, and borrowed ones are ignored
    many = np.linspace(0.2, 0.5, 10)
    assert choose_floor(top_in, many) == choose_floor(top_in, many, probe_tops=np.zeros(5))


def test_the_generated_checks_never_claim_a_training_gain_they_did_not_measure():
    from faqengine.report import verdict

    def report(training, tuned, base, lift):
        return {"selfcheck": {"systems": {"tuned": {"hit@1": tuned}, "base": {"hit@1": base}, "bm25": {"hit@1": 0.7}}, "by_source": {}, "lift": {"base->tuned": lift},
                              "off_topic": {"tuned": {"off_topic_rejected": None}}, "probes_left_out": []},
                "training": training, "faq": {"health": {"single_phrasing": 0, "entries": 5, "advice": []}}}

    worse = verdict(report({"worse_on_check": [0.75, 1.0]}, 1.0, 1.0, [0.0, 0.0, 0.0]))[0]
    assert "worse on the check questions" in worse and "uses the starting model" in worse  # a lucky dev checkpoint is not shipped on its own say-so
    assert verdict(report({"beat_untuned_on_dev": True}, 0.9, 0.8, [0.1, 0.02, 0.2]))[0].startswith("Training helped")
    chance = verdict(report({"beat_untuned_on_dev": True}, 0.85, 0.8, [0.05, -0.05, 0.15]))[0]
    assert "within chance" in chance and "not claimed" in chance  # an interval that includes zero is not an improvement
