"""The retrieval engine (runtime/retrieval.py): hybrid ranking, structured candidates, duplicates, source filtering, and the rule that a score is not a confidence."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
import retrieval as rt  # noqa: E402
from textutil import normalize_query  # noqa: E402


def unit(*v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def build():
    """Five entries on four axes; the vectors are chosen so that the model and the keywords disagree where a test needs them to."""
    entries = [
        {"id": "faq-1", "question": "What are your opening hours?", "answer": "We are open Tuesday to Sunday, 9 AM to 7 PM.", "kind": "faq", "source": "faq.csv", "category": "hours"},
        {"id": "faq-2", "question": "Do you accept UPI?", "answer": "Yes, UPI, cards and cash are accepted.", "kind": "faq", "source": "faq.csv", "category": "payment"},
        {"id": "faq-3", "question": "How do I get a refund?", "answer": "Refunds are processed within 7 business days.", "kind": "faq", "source": "faq.csv", "category": "refunds"},
        {"id": "doc-1", "question": "Late collection", "answer": "A late collection fee of ₹100 applies for each additional 30-minute period.", "kind": "passage", "source": "handbook.md", "category": None},
        {"id": "doc-2", "question": "Late collection (copy)", "answer": "A late collection fee of ₹100 applies for each additional 30-minute period.", "kind": "passage", "source": "web", "category": None},
    ]
    views = [{"text": e["question"], "entry": i, "kind": "q"} for i, e in enumerate(entries)]
    vectors = np.stack([unit(1, 0, 0, 0.2), unit(0, 1, 0, 0.2), unit(0, 0, 1, 0.2), unit(0, 0, 0.3, 1), unit(0, 0, 0.3, 1)])
    return rt.HybridIndex(entries, views, vectors)


def test_candidates_are_structured_and_the_cosine_is_not_the_ranking_score():
    ix = build()
    faq, passages, diag = ix.search("opening hours", unit(1, 0, 0, 0.2), n=3)
    top = faq[0]
    assert top["entry_id"] == "faq-1" and top["kind"] == "faq" and top["source"] == "faq.csv"
    for key in ("similarity_score", "keyword_score", "combined_score", "retrieval_method", "location", "matched", "matched_kind", "duplicates", "rank", "score"):
        assert key in top
    assert top["score"] == top["similarity_score"]  # `score` stays the cosine: the evidence engine reads it as a similarity
    assert top["keyword_score"] > 0 and top["combined_score"] == pytest.approx(1.0)  # best on both channels: the ends of the 0..1 scale
    assert "not a probability" in diag["note"] and diag["method"] == "hybrid" and diag["alpha"] == rt.DEFAULT_ALPHA


def test_a_shared_word_lifts_an_entry_the_model_ranks_lower():
    ix = build()
    q = unit(0.6, 0.7, 0, 0.2)  # the model alone prefers UPI
    sem, _, _ = ix.search("refund", q, n=3, method="semantic")
    hyb, _, _ = ix.search("refund", q, n=3, method="hybrid", alpha=0.6)
    assert sem[0]["entry_id"] in ("faq-1", "faq-2")
    assert hyb[0]["entry_id"] == "faq-3"  # "refund" is a word of exactly one entry
    assert hyb[0]["retrieval_method"] == "hybrid"
    key, _, _ = ix.search("refund", q, n=3, method="keyword")
    assert key[0]["entry_id"] == "faq-3"


def test_with_no_word_in_common_the_order_is_the_models_and_says_so():
    ix = build()
    faq, _, _ = ix.search("zzzz qqqq", unit(0, 1, 0, 0.2), n=3)
    assert faq[0]["entry_id"] == "faq-2" and faq[0]["keyword_score"] == 0.0 and "semantic only" in faq[0]["retrieval_method"]


def test_faq_entries_and_passages_are_ranked_apart_and_duplicate_text_is_collapsed():
    ix = build()
    faq, passages, diag = ix.search("late collection fee", unit(0, 0, 0.3, 1), n=2)
    assert {h["kind"] for h in faq} == {"faq"} and {h["kind"] for h in passages} == {"passage"}
    assert [h["entry_id"] for h in passages] == ["doc-1"]  # the same text crawled twice is one candidate...
    assert passages[0]["duplicates"] == ["doc-2"] and diag["duplicates_dropped"] == 1  # ...and the other id is named, not lost


def test_source_filter_limits_the_search():
    ix = build()
    faq, passages, diag = ix.search("late collection fee", unit(0, 0, 0.3, 1), n=5, allowed_sources={"handbook.md"})
    assert faq == [] and [h["entry_id"] for h in passages] == ["doc-1"] and diag["source_filter"] == ["handbook.md"]
    faq, passages, _ = ix.search("fee", unit(0, 0, 0.3, 1), n=5, allowed_sources={"nothing.pdf"})
    assert faq == [] and passages == []


def test_the_order_is_reproducible_and_a_bad_method_is_refused():
    ix = build()
    a = ix.search("payment", unit(0, 1, 0, 0.2), n=3)[0]
    b = ix.search("payment", unit(0, 1, 0, 0.2), n=3)[0]
    assert [h["entry_id"] for h in a] == [h["entry_id"] for h in b]
    with pytest.raises(ValueError):
        ix.search("x", unit(1, 0, 0, 0), method="magic")
    with pytest.raises(ValueError):
        rt.HybridIndex([], [{"text": "a", "entry": 0, "kind": "q"}], np.zeros((2, 3)))


def test_bm25_prefers_rarer_words_and_blend_ends_are_zero_and_one():
    index = rt.BM25(["refund policy", "opening hours policy", "payment policy"])
    s = index.scores("refund policy")
    assert s[0] > s[1] and s[0] > s[2] and index.scores("zzz").sum() == 0
    u = rt.unit(np.array([2.0, 3.0, 4.0]))
    assert u.min() == 0.0 and u.max() == 1.0 and rt.unit(np.array([1.0, 1.0])).max() == 0.0  # all equal -> no information, not a division by zero


def test_queries_are_normalised_before_searching():
    assert normalize_query("  What​  are \n your   HOURS?\x07 ") == "What are your HOURS?"
    assert normalize_query("ＵＰＩ ａｃｃｅｐｔｅｄ") == "UPI accepted"
    assert len(normalize_query("a" * 5000)) == 2000


def test_the_models_best_matches_stay_inside_the_pool_the_evidence_engine_reads():
    sim = np.array([0.9, 0.8, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])  # the model's favourites are entries 0 and 1
    blended = np.array([7, 6, 5, 4, 3, 2, 1, 0, ])  # a blend that puts them last
    kept = rt.guard_order(blended, sim, guard=2, pool=5)
    assert {0, 1} <= set(kept[:5].tolist()) and sorted(kept.tolist()) == list(range(8))  # nothing is lost or duplicated
    assert kept[:5].tolist() == sorted(kept[:5].tolist(), key=lambda i: blended.tolist().index(i))  # inside the pool the blend's order is kept
    already = np.array([0, 1, 2, 3, 4, 5, 6, 7])
    assert rt.guard_order(already, sim, guard=2, pool=5).tolist() == already.tolist()  # nothing to change when they are already there
    assert rt.guard_order(blended, sim, guard=0, pool=5).tolist() == blended.tolist()
    short = np.array([2, 1, 0])
    assert rt.guard_order(short, sim, guard=2, pool=5).tolist() == [2, 1, 0]  # a short list is read whole anyway


def _wide_index():
    entries = [{"id": f"f{i}", "question": f"topic number {i}", "answer": f"answer {i}", "kind": "faq", "source": "faq.csv", "category": None} for i in range(14)]
    entries.append({"id": "p0", "question": "a passage", "answer": "passage text zzz", "kind": "passage", "source": "doc.md", "category": None})
    views = [{"text": f"topic number {i} zq{i}", "entry": i, "kind": "q"} for i in range(14)] + [{"text": "a passage", "entry": 14, "kind": "q"}]
    V = np.stack([unit(*(1 if k == i else 0 for k in range(15)), 0.1) for i in range(15)])
    return rt.HybridIndex(entries, views, V)


def test_search_keeps_the_models_best_matches_inside_the_evidence_pool_whatever_their_kind():
    ix = _wide_index()
    qv = unit(*(1 if k == 0 else 0 for k in range(15)), 0.1)  # the model's best match is entry 0
    words = "zq13 zq12 zq11 zq10 zq9 zq8 zq7 zq6 zq5 zq4"  # keywords pull strongly towards the others
    faq, passages, _ = ix.search(words, qv, alpha=0.9)
    assert "f0" in [h["entry_id"] for h in faq][:rt.EVIDENCE_POOL]  # still inside the pool
    off, _, _ = ix.search(words, qv, alpha=0.9, guard=0)
    assert "f0" not in [h["entry_id"] for h in off]  # and without the guard it is not even a candidate


def test_every_kind_competes_in_one_ranking_with_no_quota_and_no_head_start():
    ix = _wide_index()
    qv = unit(*(1 if k == 14 else 0 for k in range(15)), 0.1)  # the passage is what the question is about
    faq, passages, diag = ix.search("a passage zzz", qv)
    assert [h["entry_id"] for h in passages] == ["p0"] and passages[0]["rank"] == 1  # the best candidate overall is a passage: rank 1, not "rank 1 among passages"
    assert faq and faq[0]["rank"] == 2 and diag["candidates"] == rt.CANDIDATES and diag["kinds"]["passage"] == 1  # no places are reserved for FAQ entries or for passages
    ranks = sorted(h["rank"] for h in faq + passages)
    assert ranks == list(range(1, rt.CANDIDATES + 1))


def test_entries_that_are_not_current_are_never_candidates_but_are_reported():
    entries = [{"id": "new", "question": "Refunds", "answer": "Refunds are available within 14 days.", "kind": "passage", "source": "billing.md", "category": None},
               {"id": "old", "question": "Refunds (2025)", "answer": "Refunds are available within 30 days.", "kind": "passage", "source": "old.md", "category": None, "inactive": "expired on 2025-12-31"}]
    views = [{"text": "Refunds", "entry": 0, "kind": "q"}, {"text": "Refunds (2025)", "entry": 1, "kind": "q"}]
    ix = rt.HybridIndex(entries, views, np.stack([unit(1, 0.2), unit(1, 0.25)]))
    faq, passages, diag = ix.search("refund window", unit(1, 0.25))
    assert [h["entry_id"] for h in passages] == ["new"]
    assert [m["entry_id"] for m in diag["inactive_matches"]] == ["old"] and diag["inactive_matches"][0]["inactive"] == "expired on 2025-12-31"


def test_a_name_the_knowledge_never_mentions_is_reported():
    entries = [{"id": "a", "question": "Support hours", "answer": "Support is available Monday to Friday, in English.", "kind": "passage", "source": "s.md", "category": None}]
    views = [{"text": "Support hours", "entry": 0, "kind": "q"}, {"text": "Support hours Support is available Monday to Friday, in English.", "entry": 0, "kind": "qa"}]  # a passage is searched by its heading and by heading plus text
    ix = rt.HybridIndex(entries, views, np.stack([unit(1, 0.2), unit(1, 0.3)]))
    assert ix.unknown_names("Do you offer support in Hindi?") == ["hindi"]
    assert ix.unknown_names("Do you offer support in English?") == [] and ix.unknown_names("What are the support hours?") == []
