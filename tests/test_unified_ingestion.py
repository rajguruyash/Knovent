"""Every kind of source goes through the same pipeline into the same kind of entry: parse, normalise, chunk, keep where it came from (file or address, heading), embed, and one index. These
tests cover what each kind becomes, what a source says about its own freshness, and that retrieval and answers treat what is not current accordingly.

Standard library and numpy only (no model): the vectors in the retrieval tests are hand-made."""
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
from test_sources import _xlsx  # noqa: E402

import validity  # noqa: E402
from faqengine import sources  # noqa: E402
from faqengine.kb import KB, load_kb, write_kb  # noqa: E402
from faqengine.runtime.retrieval import HybridIndex  # noqa: E402


def write(tmp, name, text):
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


# ---- what each kind of source becomes
def test_nested_json_becomes_named_passages_whose_sentences_each_name_their_subject(tmp_path):
    p = write(tmp_path, "profile.json", json.dumps({"product": "Acme Cloud", "limits": {"max_file_size_mb": 100, "audit_log_retention_days": 365},
                                                    "modules": [{"name": "Automation", "included_in": ["Pro", "Business"]}, {"name": "Reports", "included_in": ["Business"]}]}))
    kb = load_kb(p)
    by_heading = {e.question: e.answer for e in kb.entries}
    assert set(by_heading) == {"Profile", "Limits", "Modules: Automation", "Modules: Reports"}
    assert by_heading["Limits"] == "Limits, maximum file size: 100 MB. Limits, audit log retention: 365 days."  # a unit in a key name goes with the value
    assert by_heading["Modules: Automation"] == "Automation module, included in: Pro, Business."  # lists are read, records are named by their name field
    assert all(e.kind == "passage" and e.source == "profile.json" for e in kb.entries)


def test_a_json_list_of_records_is_one_passage_per_record_with_the_file_as_context(tmp_path):
    kb = load_kb(write(tmp_path, "offices.json", json.dumps([{"city": "Pune", "staff": 80, "address": "12 Baner Road, Pune"}, {"city": "Leeds", "staff": 40, "address": "4 Park Row, Leeds"}])))
    assert [e.question for e in kb.entries] == ["Pune", "Leeds"]
    assert kb.entries[0].answer == "Pune city (offices), staff: 80. Pune city (offices), address: 12 Baner Road, Pune."  # "the Pune office" can find it: the file says what the rows are rows of


def test_every_sheet_of_a_workbook_is_read_each_as_its_own_table(tmp_path):
    _xlsx(tmp_path / "book.xlsx", [("Plans", [["Plan", "Seats"], ["Pro", 5], ["Business", 25]]), ("Limits", [["Limit", "Pro", "Business"], ["Storage GB", 100, 500]])])
    kb = load_kb(tmp_path / "book.xlsx")
    texts = {e.question: e.answer for e in kb.entries}
    assert texts["Plans: Business"] == "Business plan (Plans), seats: 25."
    assert "Storage GB limit (Limits), business: 500." in texts["Limits: Storage GB"]  # the second sheet is not dropped
    assert all(e.source == "book.xlsx" for e in kb.entries)


def test_a_table_inside_a_document_is_read_with_the_document(tmp_path):
    kb = load_kb(write(tmp_path, "terms.md", "# Terms\n\n## Fees\nFees are listed below.\n\n| Plan | Fee |\n|---|---|\n| Pro | 20 USD |\n| Business | 45 USD |\n"))
    text = " ".join(e.answer for e in kb.entries)
    assert "Pro plan, fee: 20 USD." in text and "Business plan, fee: 45 USD." in text


def test_a_faq_table_a_value_table_and_a_document_make_one_knowledge_base_with_their_sources(tmp_path):
    faq = write(tmp_path, "faq.csv", "question,answer\nDo you offer a trial?,Yes, 14 days.\n")
    prices = write(tmp_path, "pricing_plans.csv", "Plan,Monthly price (USD)\nStarter,9\nPro,20\n")
    doc = write(tmp_path, "handbook.md", "# Handbook\n\n## Support\nSupport answers within 4 business hours.\n")
    kb = sources.load_sources([faq, prices, doc])
    assert {(e.kind, e.source) for e in kb.entries} == {("faq", "faq.csv"), ("passage", "pricing_plans.csv"), ("passage", "handbook.md")}
    assert len({e.id for e in kb.entries}) == len(kb.entries)  # ids never collide across files
    assert all(v.text for v in kb.views)


# ---- what a source says about itself
def test_front_matter_states_when_a_document_stops_or_starts_and_what_it_replaces(tmp_path):
    kb = load_kb(write(tmp_path, "policy.md", "---\nvalid_until: 2025-12-31\neffective: 2025-01-01\nsupersedes: old.md, older.md\n---\n# Policy\n\n## Refunds\nRefunds within 30 days.\n"))
    e = kb.entries[0]
    assert (e.valid_from, e.valid_until, e.supersedes) == ("2025-01-01", "2025-12-31", "old.md, older.md")
    assert "---" not in e.answer and "valid_until" not in e.answer  # the header is not content
    roundtrip = tmp_path / "kb.json"
    write_kb(kb.entries, roundtrip)
    again = load_kb(roundtrip).entries[0]
    assert (again.valid_from, again.valid_until, again.supersedes) == (e.valid_from, e.valid_until, e.supersedes)  # kept when a project is saved and loaded


def test_a_header_that_is_not_a_date_is_ignored_with_a_note_and_the_text_is_still_read(tmp_path):
    kb = sources.load_sources([write(tmp_path, "later.md", "---\nvalid_until: someday\n---\n# T\n\n## S\nText here is still searchable.\n")])
    assert kb.entries and any("not a date" in w for w in kb.warnings)
    sources.resolve_validity(kb.entries, "2030-01-01")
    assert kb.entries[0].inactive is None  # nothing is guessed: an unreadable date retires nothing


def test_expired_not_yet_valid_and_replaced_sources_are_marked_but_a_replacement_must_be_in_force(tmp_path):
    old = write(tmp_path, "prices_v1.md", "# Prices\n\n## Shipping\nShipping costs 5 pounds.\n")
    new = write(tmp_path, "prices_v2.md", "---\nvalid_from: 2026-07-01\nsupersedes: prices_v1.md\n---\n# Prices\n\n## Shipping\nShipping costs 7 pounds.\n")
    stale = write(tmp_path, "offer.md", "---\nvalid_until: 2026-03-31\n---\n# Offer\n\n## Spring\n10 percent off.\n")
    kb = sources.load_sources([old, new, stale])
    before = sources.resolve_validity(kb.entries, "2026-06-15")  # the new price is announced, not in force
    why = {e.source: e.inactive for e in kb.entries}
    assert why["prices_v1.md"] is None and why["prices_v2.md"] == "not valid before 2026-07-01" and why["offer.md"] == "expired on 2026-03-31"
    assert len(before) == 2
    sources.resolve_validity(kb.entries, "2026-07-02")
    why = {e.source: e.inactive for e in kb.entries}
    assert why["prices_v1.md"] == "superseded by prices_v2.md" and why["prices_v2.md"] is None


def test_validity_rules_read_dates_strictly():
    assert validity.parse_date("2026-12-31") == date(2026, 12, 31)
    for bad in ("31/12/2026", "2026-13-01", "soon", "", None, "2026-1-5"):
        assert validity.parse_date(bad) is None
    items = [{"id": "a", "source": "a.md", "valid_until": "2026-01-31"}, {"id": "b", "source": "docs/b.md", "supersedes": "A.md"}]
    assert validity.inactive_reasons(items, "2026-02-01") == {"a": "expired on 2026-01-31"}  # already out of date: it needs no replacing and b replaces nothing it does not name
    assert validity.inactive_reasons(items, "2026-01-15") == {"a": "superseded by docs/b.md"}  # the replacement is in force, the name is compared without folders or case


# ---- retrieval and answers
def toy_index(entries, vectors):
    views = [{"text": e["question"], "entry": i, "kind": "q"} for i, e in enumerate(entries)]
    return HybridIndex(entries, views, np.array(vectors, dtype=np.float32))


def test_what_is_not_current_is_never_a_candidate_but_is_reported_so_a_question_can_be_told_why():
    live = {"id": "p-1", "question": "Shipping cost", "answer": "Shipping costs 7 pounds.", "kind": "passage", "source": "prices_v2.md", "category": None}
    dead = {"id": "p-2", "question": "Shipping cost", "answer": "Shipping costs 5 pounds.", "kind": "passage", "source": "prices_v1.md", "category": None, "inactive": "superseded by prices_v2.md"}
    index = toy_index([live, dead], [[1.0, 0.0], [0.99, 0.14]])
    faq, passages, diag = index.search("shipping cost", np.array([1.0, 0.0], np.float32))
    assert [h["entry_id"] for h in passages] == ["p-1"]
    assert [m["source"] for m in diag["inactive_matches"]] == ["prices_v1.md"] and diag["inactive_matches"][0]["inactive"] == "superseded by prices_v2.md"


def test_nothing_gets_a_head_start_for_being_a_faq_entry_or_a_passage():
    faq_entry = {"id": "faq-1", "question": "Shipping cost?", "answer": "Shipping costs 7 pounds.", "kind": "faq", "source": "faq.csv", "category": None}
    passage = {"id": "p-1", "question": "Shipping cost", "answer": "Shipping costs 7 pounds.", "kind": "passage", "source": "prices.md", "category": None}
    views = [{"text": "Shipping cost?", "entry": 0, "kind": "q"}, {"text": "Shipping cost", "entry": 1, "kind": "q"}]
    for first, second in (([1.0, 0.0], [0.9, 0.436]), ([0.9, 0.436], [1.0, 0.0])):  # whichever is closer wins, whatever it is
        index = HybridIndex([faq_entry, passage], views, np.array([first, second], np.float32))
        faq, passages, _ = index.search("shipping cost", np.array([1.0, 0.0], np.float32))
        ranked = sorted([(h["combined_score"], h["entry_id"]) for h in faq + passages], reverse=True)
        assert ranked[0][1] == ("faq-1" if first[0] == 1.0 else "p-1")


def test_a_customer_is_told_when_the_only_information_is_out_of_date_and_it_is_not_used():
    from faqengine.runtime import answer

    dead = {"id": "p-2", "question": "Return window", "answer": "Items can be returned within 14 days of delivery.", "kind": "passage", "source": "returns_2025.md", "category": None,
            "inactive": "expired on 2026-03-31"}
    index = toy_index([dead], [[1.0, 0.0]])
    sim = lambda part, texts: [0.9 for _ in texts]  # noqa: E731
    r = answer.respond("Within how many days can items be returned?", lambda q: index.search(q, np.array([1.0, 0.0], np.float32)), sim, None, 0.4, 0.45)
    assert r["outcome"] in ("insufficient", "clarification") and r.get("answer") is None and "14 days" not in (r.get("message") or "")
    assert r["outdated"] == [{"source": "returns_2025.md", "reason": "expired on 2026-03-31", "section": "Return window"}]
    assert "out of date" in r["message"] and "I have not used it" in r["message"]


def test_a_source_that_cannot_be_read_is_a_clear_error_not_a_corrupt_knowledge_base(tmp_path):
    good = write(tmp_path, "good.md", "# G\n\n## Fact\nThe warranty lasts 2 years.\n")
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"PK\x03\x04 not really a workbook")
    with pytest.raises(ValueError):
        load_kb(broken)
    assert [e.answer for e in load_kb(good).entries] == ["The warranty lasts 2 years."]  # the others are unaffected
    assert isinstance(KB(load_kb(good).entries), KB)
