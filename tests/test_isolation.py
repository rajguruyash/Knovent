"""Test questions are evaluation data only: they must never become FAQ entries, documents, training examples, vocabulary or index views.

Root cause this guards against: a file of test questions (header `question,expected_...`) was accepted as a FAQ source because a `question` column is all
the FAQ reader asked for, so 185 test questions became 185 searchable, trainable entries and every score measured the engine finding its own input.
"""
import json
import urllib.error
from pathlib import Path

import pytest

from faqengine.heldout import load_heldout, normalize
from faqengine.kb import load_kb

PAWFECT = Path(__file__).resolve().parents[1] / "examples" / "pawfect"
SOURCES = [PAWFECT / "pawfect_faq.csv", PAWFECT / "pawfect_handbook.md"]
TEST_FILE = PAWFECT / "heldout_questions.original.csv"  # the user's own file, unchanged: 185 rows, 121 unique


def labels():
    return json.loads((PAWFECT / "labels.json").read_text())


def test_a_file_of_test_questions_is_refused_as_a_knowledge_source_by_every_reader(tmp_path):
    for path in (TEST_FILE, Path(__file__).resolve().parents[1] / "examples" / "washo" / "heldout.csv", Path(__file__).resolve().parents[1] / "examples" / "domains" / "clinic" / "heldout.csv"):
        with pytest.raises(ValueError, match="looks like a list of test questions"):
            load_kb(path)
        with pytest.raises(ValueError, match="Held-out test questions"):
            load_kb([*SOURCES, path])  # nor can it ride along with real sources
    renamed = tmp_path / "my_faq.csv"  # the name proves nothing: the columns decide
    renamed.write_text(TEST_FILE.read_text())
    with pytest.raises(ValueError, match="looks like a list of test questions"):
        load_kb(renamed)
    assert len(load_kb(SOURCES)) == 10 + 7  # real FAQs and documents are untouched by the guard
    ok = tmp_path / "answers_given.csv"  # a real FAQ that happens to have a `label` column is still a FAQ
    ok.write_text("question,answer,label\nHow do I pay?,By card.,billing\n")
    assert load_kb(ok).entries[0].answer == "By card."


def test_the_studio_refuses_test_questions_as_sources_and_keeps_them_out_of_the_knowledge_base(tmp_path):
    from faqengine.studio.store import Store

    store = Store(tmp_path)
    with pytest.raises(ValueError, match="test questions"):
        store.create_many("x", [(TEST_FILE.name, TEST_FILE.read_bytes())])
    pid = store.create_many("pawfect", [(p.name, p.read_bytes()) for p in SOURCES])
    with pytest.raises(ValueError, match="test questions"):
        store.add_sources(pid, [(TEST_FILE.name, TEST_FILE.read_bytes())])
    assert [r["name"] for r in store.sources(pid)] == ["pawfect_faq.csv", "pawfect_handbook.md"]  # nothing was added

    store.set_heldout(pid, (PAWFECT / "heldout.csv").read_text())
    kb = store.kb(pid)
    held = {normalize(q) for q in load_heldout(PAWFECT / "heldout.csv", kb).queries}
    indexed = {normalize(v.text) for v in kb.views}
    assert not held & indexed  # not one held-out question is an entry, a phrasing, a heading or a view of the index
    assert len(kb) == 17 and store.faq_summary(pid)["entries"] == 10


def test_a_project_that_already_holds_a_test_file_as_a_source_is_repaired(tmp_path):
    from faqengine.studio.store import Store

    store = Store(tmp_path)
    pid = store.create_many("legacy", [(p.name, p.read_bytes()) for p in SOURCES])
    d = store.dir(pid)
    (d / "sources" / "heldout_questions.csv").write_bytes(TEST_FILE.read_bytes())  # how the old version let it in
    store.set_meta(pid, sources=[*store.sources(pid), {"name": "heldout_questions.csv", "file": "sources/heldout_questions.csv", "kind": "faq", "size": 1, "added": 0}])
    notes = store.ensure_isolation(pid)
    assert notes and "held-out test set" in notes[0]
    assert [r["name"] for r in store.sources(pid)] == ["pawfect_faq.csv", "pawfect_handbook.md"]
    assert not (d / "sources" / "heldout_questions.csv").exists() and (d / "heldout.csv").read_bytes() == TEST_FILE.read_bytes()
    assert len(store.kb(pid)) == 17 and store.ensure_isolation(pid) == []  # and it is done once


def test_duplicates_are_removed_and_counted_never_used_to_pad_the_set():
    kb = load_kb(SOURCES)
    ho = load_heldout(TEST_FILE, kb, labels=labels())
    c = ho.counts()
    assert c["rows_read"] == 185 and c["unique_questions"] == 121 and c["total"] == 121 and c["exact_duplicates_removed"] == 64
    assert len({normalize(q) for q in ho.queries}) == len(ho.queries)  # no question is in twice
    assert c["abstentions"] == 31 and c["expected_entries"] == 16 and "handbook_skin" in c["topics"]


def test_every_question_needs_a_valid_expectation(tmp_path):
    kb = load_kb(SOURCES)

    def attempt(text, **kw):
        path = tmp_path / "t.csv"
        path.write_text(text)
        return load_heldout(path, kb, **kw)

    with pytest.raises(ValueError, match="closest: faq-008"):  # free-text sections are never guessed; the closest entries are suggested
        attempt("question,expected\nCan I stay with my dog?,FAQ: owner presence during grooming\n")
    with pytest.raises(ValueError, match="no expected entry"):
        attempt("query,entry_id,type\nWhere are you?,,paraphrase\n")
    with pytest.raises(ValueError, match="different expectations"):  # the same question labelled two ways is a data error, not something to pick between
        attempt("query,entry_id,type\nWhere are you?,faq-002,paraphrase\nwhere are you,faq-003,paraphrase\n")
    ok = attempt("question,expected_behavior,expected_entry\nWho won the match?,abstain,\nwho won the match?,abstain,\nWhere are you?,answer,faq-002\n")
    assert ok.types == ["out-of-scope", "question"] and ok.counts()["exact_duplicates_removed"] == 1
    assert attempt("query,entry_id,type\nx y z,Opening schedule,paraphrase\n").gold[0] == frozenset(i for i, e in enumerate(kb.entries) if e.question == "Opening schedule")  # a heading names its passages


def test_held_out_questions_that_copy_a_training_phrasing_are_dropped_and_reported(tmp_path):
    kb = load_kb(SOURCES)
    path = tmp_path / "t.csv"
    path.write_text("query,entry_id,type\nWhat payment methods do you accept?,faq-009,paraphrase\nWhich ways of paying do you take?,faq-009,paraphrase\n")
    ho = load_heldout(path, kb)
    assert ho.queries == ["Which ways of paying do you take?"] and ho.counts()["removed_for_leakage"] == 1  # a test question the model trained on tests nothing
