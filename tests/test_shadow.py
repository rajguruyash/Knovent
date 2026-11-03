"""The comparison of the existing path and the pipeline (runtime/shadow.py): what counts as 'the same answer', and that the two allowances it makes are narrow."""
from faqengine.runtime import shadow


def result(**kw):
    base = {"outcome": "grounded", "answer": "Pro costs 20 USD. [1]", "sources": [{"id": "a"}], "claims": [{"text": "Pro costs 20 USD."}], "conflicts": [], "unanswered_parts": [],
            "retrieval": {"candidates": [{"entry_id": "a"}, {"entry_id": "b"}], "diagnostics": {}}, "execution": {"retrieval_passes": 1}}
    return {**base, **kw}


def test_identical_results_agree():
    assert shadow.compare(result(), result()) == []


def test_every_kind_of_difference_is_named():
    assert shadow.compare(result(), result(outcome="partial")) == ["outcome: grounded vs partial"]
    assert "answer differs" in shadow.compare(result(), result(answer="Pro costs 21 USD. [1]"))
    assert "cited sources differ" in shadow.compare(result(), result(sources=[{"id": "b"}]))
    assert "claims differ" in shadow.compare(result(), result(claims=[{"text": "Pro costs 21 USD."}]))
    assert "conflicts differ" in shadow.compare(result(), result(conflicts=[{"sources": []}]))
    assert "unanswered parts differ" in shadow.compare(result(), result(unanswered_parts=["x"]))
    assert "structured results differ" in shadow.compare(result(computed=[{"op": "min"}]), result(computed=[{"op": "max"}]))
    stale = {"candidates": [{"entry_id": "a"}], "diagnostics": {"inactive_matches": [{"entry_id": "old"}]}}
    assert any("freshness" in d for d in shadow.compare(result(retrieval=stale), result(retrieval={**stale, "diagnostics": {"inactive_matches": []}})))


def test_a_targeted_second_pass_may_add_candidates_at_the_end_and_nothing_else():
    legacy = result()
    more = [{"entry_id": "a"}, {"entry_id": "b"}, {"entry_id": "c"}]
    added = result(retrieval={"candidates": more, "diagnostics": {}}, execution={"retrieval_passes": 2})
    assert shadow.compare(legacy, added) == []
    one_pass = result(retrieval={"candidates": more, "diagnostics": {}}, execution={"retrieval_passes": 1})
    assert shadow.compare(legacy, one_pass) == ["retrieved candidates differ"]  # without the second pass, extra candidates are a difference
    reordered = result(retrieval={"candidates": [{"entry_id": "b"}, {"entry_id": "a"}, {"entry_id": "c"}], "diagnostics": {}}, execution={"retrieval_passes": 2})
    assert shadow.compare(legacy, reordered) == ["retrieved candidates differ"]
    dropped = result(retrieval={"candidates": [{"entry_id": "a"}], "diagnostics": {}}, execution={"retrieval_passes": 2})
    assert shadow.compare(legacy, dropped) == ["retrieved candidates differ"]  # a candidate the first search found may not disappear


def test_a_conflict_is_read_by_what_each_side_says_on_both_paths_and_only_for_conflicts():
    sides = [{"sources": [{"text": "Invoices are payable within 30 days."}, {"text": "Invoices are payable within 15 days."}]}]
    legacy = result(outcome="conflict", answer=None, claims=[], conflicts=sides)
    pipeline = result(outcome="conflict", answer=None, conflicts=sides, claims=[{"text": "Invoices are payable within 30 days."}, {"text": "Invoices are payable within 15 days."}])
    assert shadow.compare(legacy, pipeline) == []
    lost = result(outcome="conflict", answer=None, conflicts=sides, claims=[{"text": "Invoices are payable within 30 days."}])
    assert shadow.compare(legacy, lost) == ["claims differ"]  # a side that was dropped is a difference
    assert shadow.compare(result(claims=[]), result()) == ["claims differ"]  # outside conflicts, an empty list is not read as anything


# ---- naming what a difference is about (this decides nothing about what counts as one)
def kinds(legacy, new):
    return shadow.categories(shadow.compare(legacy, new))


def test_every_difference_has_a_kind_a_reader_can_act_on():
    assert kinds(result(), result(answer="Pro costs 21 USD. [1]")) == ["answer"]
    assert kinds(result(), result(claims=[{"text": "x"}])) == ["claims"]
    assert kinds(result(), result(sources=[{"id": "b"}])) == ["citations"]
    assert kinds(result(), result(conflicts=[{"sources": []}])) == ["conflict"]
    assert kinds(result(computed=[{"op": "min"}]), result(computed=[{"op": "max"}])) == ["structured"]
    assert kinds(result(), result(retrieval={"candidates": [{"entry_id": "z"}], "diagnostics": {}})) == ["evidence"]
    assert kinds(result(unanswered_parts=[]), result(unanswered_parts=["part"])) == ["abstention"]
    assert set(kinds(result(), result(outcome="insufficient", answer=None, sources=[], claims=[]))) == {"abstention", "answer", "claims", "citations"}  # answered on one path, abstained on the other
    assert kinds(result(), result(outcome="partial")) == ["answer"]  # answered both ways: a different kind of answer, not an abstention
    assert shadow.categories(["tenant context differs: evidence from another business"]) == ["tenant"]
    assert set(shadow.categories(["a", "b"])) == {"answer"}  # an unfamiliar phrase is still counted, as an answer difference, never dropped


def test_percentiles():
    assert shadow.percentile([], 50) is None and shadow.percentile([7], 95) == 7
    assert shadow.percentile([1, 2, 3, 4, 5], 50) == 3 and shadow.percentile([1, 2, 3, 4, 5], 100) == 5 and shadow.percentile([10, 20], 50) == 15
    assert abs(shadow.percentile(list(range(1, 101)), 95) - 95.05) < 1e-9


def test_strict_switches_off_both_allowances_and_nothing_else():
    more = [{"entry_id": "a"}, {"entry_id": "b"}, {"entry_id": "c"}]
    added = result(retrieval={"candidates": more, "diagnostics": {}}, execution={"retrieval_passes": 2})
    assert shadow.compare(result(), added) == [] and shadow.compare(result(), added, strict=True) == ["retrieved candidates differ"]
    sides = [{"sources": [{"text": "Invoices are payable within 30 days."}]}]
    legacy = result(outcome="conflict", answer=None, claims=[], conflicts=sides)
    pipeline = result(outcome="conflict", answer=None, conflicts=sides, claims=[{"text": "Invoices are payable within 30 days."}])
    assert shadow.compare(legacy, pipeline) == [] and shadow.compare(legacy, pipeline, strict=True) == ["claims differ"]
    assert shadow.compare(result(), result(), strict=True) == [] and shadow.compare(result(), result(answer="x"), strict=True) == ["answer differs"]
