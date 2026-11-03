"""A table's columns can be chosen by hand in the Studio: nothing about the header names has to match, the uploaded file is kept, a bad choice is undone."""
import json
import urllib.error

import pytest

from faqengine.studio.store import Store

PRICING = b"Plan,Monthly price (USD),Seats\nStarter,9,1\nPro,20,5\nBusiness,45,25\n"
WEIRD = b"Col A,Col B,Col C\nWhat time do you open?,Nine to five.,Hours\nWhere are you?,Main Street.,Place\n"
DOC = b"# Returns\n\nItems can be returned within thirty days of delivery with the receipt.\n"


@pytest.fixture()
def project(tmp_path):
    store = Store(tmp_path)
    pid = store.create_many("columns", [("pricing_plans.csv", PRICING), ("weird.csv", WEIRD), ("returns.md", DOC)])
    return store, pid


def sources(store, pid):
    return {s["name"]: s for s in store.faq_summary(pid)["sources"]}


def test_any_header_names_are_read_and_a_values_table_is_searchable_row_by_row(project):
    store, pid = project
    got = sources(store, pid)
    assert got["pricing_plans.csv"]["kind"] == "records" and got["pricing_plans.csv"]["passages"] == 3 and got["pricing_plans.csv"]["table"] is True
    assert got["weird.csv"]["kind"] == "faq" and got["weird.csv"]["faq_entries"] == 2  # a column of questions is found by what it holds
    assert got["returns.md"]["table"] is False
    rows = {e["question"] for e in store.faq_summary(pid)["preview"]}
    assert {"Starter", "Pro", "Business", "What time do you open?"} <= rows


def test_the_columns_of_a_table_are_described_and_a_document_has_none(project):
    store, pid = project
    info = store.table_info(pid, "weird.csv")
    assert info["columns"] == ["col_a", "col_b", "col_c"] and info["mode"] == "faq" and info["detected"]["question"] == "col_a" and info["mapping"] is None and info["rows"] == 2
    with pytest.raises(ValueError, match="document"):
        store.table_info(pid, "returns.md")
    with pytest.raises(KeyError):
        store.table_info(pid, "nothing.csv")


def test_a_choice_is_applied_to_a_copy_and_the_uploaded_file_is_kept(project):
    store, pid = project
    d = store.dir(pid)
    store.apply_mapping(pid, "weird.csv", {"as": "faq", "question": "col_a", "answer": "col_b", "category": "col_c"})
    kb = store.kb(pid)
    entry = next(e for e in kb.entries if e.questions[0] == "What time do you open?")
    assert entry.answer == "Nine to five." and entry.category == "Hours"
    assert (d / "sources" / "originals" / "weird.csv").read_bytes() == WEIRD  # what was uploaded is untouched
    rec = next(r for r in store.sources(pid) if r["name"] == "weird.csv")
    assert rec["mapping"]["as"] == "faq" and rec["original"] == "sources/originals/weird.csv" and sources(store, pid)["weird.csv"]["mapped"] is True
    # a second choice starts again from the original, not from the copy
    store.apply_mapping(pid, "weird.csv", {"as": "records", "title": "col_c"})
    got = sources(store, pid)["weird.csv"]
    assert got["kind"] == "records" and got["passages"] == 2 and got["faq_entries"] == 0
    assert store.table_info(pid, "weird.csv")["mapping"]["as"] == "records" and store.table_info(pid, "weird.csv")["columns"] == ["col_a", "col_b", "col_c"]


def test_a_choice_that_cannot_work_is_refused_and_changes_nothing(project):
    store, pid = project
    before = sources(store, pid)["weird.csv"], (store.dir(pid) / "sources" / "weird.csv").read_bytes()
    for bad in ({"as": "faq"}, {"as": "faq", "question": "nope"}, {"as": "documents"}, {"as": "sideways"}, {"as": "faq", "question": "col_a", "answer": "col_a"}):
        with pytest.raises(ValueError):
            store.apply_mapping(pid, "weird.csv", bad)
    assert (sources(store, pid)["weird.csv"], (store.dir(pid) / "sources" / "weird.csv").read_bytes()) == before
    with pytest.raises(ValueError, match="document"):
        store.apply_mapping(pid, "returns.md", {"as": "faq", "question": "x"})


def test_changing_the_columns_makes_a_trained_model_stale_and_removing_the_file_removes_its_original(project):
    store, pid = project
    sig = store.signature(pid)
    store.apply_mapping(pid, "pricing_plans.csv", {"as": "records", "title": "plan"})
    assert store.signature(pid) != sig  # the project says it must be trained again
    original = store.dir(pid) / "sources" / "originals" / "pricing_plans.csv"
    assert original.exists()
    store.remove_source(pid, "pricing_plans.csv")
    assert not original.exists() and "pricing_plans.csv" not in sources(store, pid)


def test_the_routes_work_over_http(site):
    def post(path, body):
        try:
            return 200, json.loads(site.call("POST", path, body))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    pid = site.j("POST", "/api/projects", {"name": "cols", "files": [{"filename": "weird.csv", "text": WEIRD.decode()}, {"filename": "pricing_plans.csv", "text": PRICING.decode()}]})["id"]
    info = site.j("POST", f"/api/projects/{pid}/columns", {"name": "weird.csv"})
    assert info["columns"] == ["col_a", "col_b", "col_c"] and info["mode"] == "faq"
    assert post(f"/api/projects/{pid}/columns/apply", {"name": "weird.csv", "mapping": {"as": "faq"}})[0] == 400
    assert post(f"/api/projects/{pid}/columns/apply", {"name": "missing.csv", "mapping": {"as": "faq", "question": "col_a"}})[0] == 404
    assert post(f"/api/projects/{pid}/columns/apply", {"name": "weird.csv", "mapping": {"as": "faq", "question": "col_a", "answer": "col_b"}})[0] == 200
    summary = site.j("GET", f"/api/projects/{pid}")["faq"]["sources"]
    assert {s["name"]: (s["kind"], s["mapped"]) for s in summary} == {"weird.csv": ("faq", True), "pricing_plans.csv": ("records", False)}
