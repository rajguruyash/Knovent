"""The hosted measurement (hosted/measure.py): hold out one wording at a time, and see where its entry lands from the others. Numbers checked by hand on a tiny index."""
import numpy as np

from faqengine.hosted.measure import measure
from faqengine.runtime.retrieval import HybridIndex


def entry(i, q, kind="faq"):
    return {"id": f"e{i}", "question": q, "answer": f"answer {i}", "kind": kind, "source": "faq.csv", "source_name": "faq.csv", "category": None}


def build(spec):
    """spec: list of (entry index, wording text, vector); returns a HybridIndex with the views grouped by entry."""
    n = max(e for e, _, _ in spec) + 1
    entries = [entry(i, next(t for e, t, _ in spec if e == i)) for i in range(n)]
    spec = sorted(spec, key=lambda s: s[0])
    views = [{"text": t, "entry": e, "kind": "q"} for e, t, _ in spec]
    return HybridIndex(entries, views, np.array([v for _, _, v in spec], dtype=np.float32))


def test_each_wording_is_held_out_and_ranked_against_the_other_entries():
    a, b, c = np.array([1, 0, 0]), np.array([0, 1, 0]), np.array([0, 0, 1])
    near_a = np.array([0.9, 0.1, 0])          # a second wording of entry 0 that sits next to a
    toward_b = np.array([0.2, 0.9, 0])        # a second wording of entry 1 that sits next to b
    stray = np.array([0.6, 0.65, 0.2])         # a second wording of entry 2 that is nearer to a and b than to c
    ix = build([(0, "opening hours", a), (0, "when are you open", near_a), (1, "refund time", b), (1, "how long for a refund", toward_b), (2, "delivery cost", c), (2, "shipping price", stray)])
    r = measure(ix)
    assert r["checks"] == 6 and r["entries"] == 3 and r["views"] == 6 and r["training"] is None and r["tuned"] is False
    base = r["systems"]["base"]
    # entries 0 and 1 are found first from their other wording; entry 2's wordings are found from each other only at rank 3 (c is orthogonal to the rest, stray is nearer a and b)
    # held out: a->near_a first (1); near_a->a first (1); b->toward_b (1); toward_b->b (1); c->stray: stray scores 0.2*1 on c, a 0.0, b 0.0 so entry 2 is first (1); stray->c: 0.2 vs a 0.6, b 0.65 -> rank 3
    assert base["hit@1"] == 5 / 6 and base["hit@3"] == 1.0
    assert abs(base["mrr@10"] - (5 + 1 / 3) / 6) < 1e-9
    assert abs(base["ndcg@10"] - (5 + 1 / np.log2(4)) / 6) < 1e-9


def test_a_wording_never_finds_its_own_entry_by_matching_itself():
    v = np.array([1, 0, 0])
    ix = build([(0, "alpha", v), (0, "beta", np.array([0, 1, 0])), (1, "gamma", np.array([0, 0, 1])), (1, "delta", np.array([0, 0, 1]))])
    r = measure(ix)
    # entry 0's two wordings are orthogonal: each, held out, leaves the other wording at 0 for its own entry, while entry 1 also scores 0: a tie, not a win
    assert r["systems"]["base"]["hit@1"] < 1.0


def test_entries_with_one_wording_and_documents_cannot_be_checked_and_it_says_so():
    ix = build([(0, "only wording", np.array([1, 0, 0])), (1, "also one", np.array([0, 1, 0]))])
    r = measure(ix)
    assert r["checks"] == 0 and r["systems"] == {} and "Nothing to measure yet" in r["verdict"][0]
    ix = build([(0, "a b", np.array([1, 0, 0])), (0, "a c", np.array([1, 0.1, 0])), (1, "solo", np.array([0, 1, 0]))])
    r = measure(ix)
    assert r["checks"] == 2 and any("only one wording" in v for v in r["verdict"])


def test_a_large_knowledge_base_is_sampled_not_slow():
    rng = np.random.default_rng(0)
    spec = [(e, f"word{e} extra{k}", rng.normal(size=16)) for e in range(900) for k in range(2)]
    r = measure(build(spec))
    assert r["checks"] == 1500 and 0.0 <= r["systems"]["base"]["hit@1"] <= 1.0
