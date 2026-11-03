"""One knowledge base of many kinds of source, hosted: the whole Acme example (a FAQ table, Markdown, a spreadsheet, JSON, CSV, web pages) goes through the hosted service into the real
schema, and a customer asks questions that none of the sources answers alone. Also what the hosted side adds: freshness (expired, not yet valid, replaced), deleting a source, reading it again
without duplicates, the unified read model, and that no business ever sees another's knowledge, measured over every question of the example sets.

Needs the exported embedding model and a local PostgreSQL with pgvector (skipped otherwise). Not covered: a live Supabase project."""
import csv
import sys
from datetime import date
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("cryptography")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))
from pgdb import make_db  # noqa: E402

import unified_benchmark as UB  # noqa: E402
from faqengine.hosted import service as svc  # noqa: E402
from faqengine.hosted.secrets import SecretBox  # noqa: E402
from faqengine.hosted.store import Store  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
pytestmark = pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
KNOWLEDGE = ROOT / "examples" / "unified" / "knowledge"
DAY = date(2026, 6, 1)  # the day the example sets were written for (see scripts/unified_benchmark.py)


class Files(dict):
    def __call__(self, path):
        if path not in self:
            raise svc.ServiceError(404, "the uploaded file could not be read from storage")
        return self[path]


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def clock():
    return {"day": DAY}


@pytest.fixture(scope="module")
def engine(db, clock):
    box = SecretBox({1: SecretBox.generate() and __import__("base64").b64decode(SecretBox.generate())})
    return svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), rate_per_minute=100_000, today=lambda: clock["day"])


def add(db, engine, business, owner, name, data, kind="document"):
    engine.fetch_object[f"{business}/{name}"] = data
    sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, %s, %s, %s) returning id", (business, name, kind, f"{business}/{name}"))[0][0])
    return sid, engine.ingest(business, owner, sid)


def ask(engine, business, owner, question):
    return engine.respond(business, owner, question)["result"]


@pytest.fixture(scope="module")
def acme(db, engine):
    """The whole Acme example ingested through the hosted service, each file as its own source."""
    owner = str(db.user("u-owner"))
    biz = str(db.business(owner, "acme-unified"))
    sources, failed = {}, {}
    for p in sorted(KNOWLEDGE.rglob("*")):
        if p.is_file() and p.suffix.lower() in UB.sources.SUPPORTED:
            try:
                sources[p.name] = add(db, engine, biz, owner, p.name, p.read_bytes(), "faq_table" if p.name == "faq.csv" else "document")
            except svc.ServiceError as e:
                failed[p.name] = e.message
    return type("A", (), {"biz": biz, "owner": owner, "sources": sources, "failed": failed})


def dataset(name):
    return [dict(r) for r in csv.DictReader(open(ROOT / "examples" / "unified" / f"{name}.csv", encoding="utf-8"))]


def judged(engine, acme, rows):
    out = []
    for row in rows:
        r = ask(engine, acme.biz, acme.owner, row["question"])
        out.append((row, r, UB.judge(row, r)))
    return out


# ---- the example, end to end
def test_every_kind_of_source_is_read_into_one_knowledge_base(acme, db):
    assert acme.failed == {}
    units = db.su("select unit_kind, source_name, heading, content, version, ingested_at is not null from public.knowledge_units where business_id = %s", (acme.biz,))
    assert {u[0] for u in units} == {"faq", "passage"} and len({u[1] for u in units}) >= 10
    assert all(u[3] and u[4] == 1 and u[5] for u in units)  # every unit has its text, its version and when it was read
    for name in ("faq.csv", "pricing_plans.csv", "integrations.xlsx", "product_profile.json", "company_profile.md", "acme-status.html"):
        assert any(u[1] == name for u in units), name


def test_a_customer_gets_grounded_cited_answers_without_knowing_the_kind_of_source(acme, engine):
    r = ask(engine, acme.biz, acme.owner, "How much is the Pro plan?")
    assert r["outcome"] == "grounded" and "20 USD" in r["answer"] and r["sources"][0]["source"] == "pricing_plans.csv"
    r = ask(engine, acme.biz, acme.owner, "What is the address of the Pune office?")
    assert "Baner Road" in r["answer"] and r["sources"][0]["source"] == "offices.json"
    r = ask(engine, acme.biz, acme.owner, "When does scheduled maintenance happen?")
    assert "02:00" in r["answer"] and "acme.example/status" in r["sources"][0]["source"]  # a web page keeps its own address as its source
    r = ask(engine, acme.biz, acme.owner, "Do you sell bicycles?")
    assert r["outcome"] in ("insufficient", "clarification") and r["answer"] is None


def test_the_hosted_service_answers_the_example_sets_as_the_local_benchmark_does(acme, engine):
    # the same questions, the same code, but through the database (stored passages, stored vectors, the day set by the clock): the result must not be worse than the in-memory measurement
    for name in ("dev", "regression"):
        rows = dataset(name)
        results = judged(engine, acme, rows)
        correct = sum(j["correct"] for _, _, j in results) / len(rows)
        local = {"dev": 0.90, "regression": 0.75}[name]
        assert correct >= local, (name, correct, [(r["id"], j["outcome"]) for r, _, j in results if not j["correct"]])
        assert sum(j["outdated_used"] for _, _, j in results) == 0  # nothing expired or replaced was used to answer


def test_every_answer_is_traceable_to_stored_rows_of_this_business(acme, engine, db):
    for q in ("How much is the Pro plan?", "How long does a refund take?", "Which plans include the Automation module?", "What is the address of the Pune office?"):
        r = ask(engine, acme.biz, acme.owner, q)
        for s in r["sources"]:
            assert s["db_id"], (q, s)
            owner = db.su("select business_id from public.faqs where id = %s union all select business_id from public.document_chunks where id = %s", (s["db_id"], s["db_id"]))
            assert [str(o[0]) for o in owner] == [acme.biz]


# ---- no business ever sees another's knowledge
def test_another_business_never_receives_this_ones_knowledge_over_every_example_question(acme, engine, db):
    other_owner = str(db.user("u-other"))
    other = str(db.business(other_owner, "groomer-unified"))
    add(db, engine, other, other_owner, "pets.csv", b"id,question,answer\np1,Do you groom cats?,We groom dogs only.\np2,What are your prices?,A bath costs 600 rupees.\n", "faq_table")
    add(db, engine, other, other_owner, "handbook.md", b"# Handbook\n\n## Hours\n\nThe studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM.\n")
    mine = {r[0] for r in db.su("select id from public.faqs where business_id = %s union all select id from public.document_chunks where business_id = %s", (acme.biz, acme.biz))}
    theirs_names = {"pets.csv", "handbook.md"}
    asked = dataset("dev") + dataset("regression") + dataset("locked")  # (only counted and checked for leaks: no result of the locked set is read)
    leaks = 0
    for row in asked:
        res = engine.respond(other, other_owner, row["question"])["result"]
        names = {s["source"] for s in res.get("sources") or []} | {c["source"] for c in res["retrieval"]["candidates"]}
        leaks += len(names - theirs_names)
        assert not any(s.get("db_id") in {str(m) for m in mine} for s in res.get("sources") or [])
        assert "Acme" not in (res.get("answer") or "") and "Pro plan" not in (res.get("answer") or "")
    assert leaks == 0, "another business's questions returned sources that are not its own"
    # and the other way round: this business never receives theirs
    for q in ("Do you groom cats?", "What time does the studio open?"):
        res = ask(engine, acme.biz, acme.owner, q)
        assert "dogs only" not in (res.get("answer") or "") and "600 rupees" not in (res.get("answer") or "")
        assert not ({c["source"] for c in res["retrieval"]["candidates"]} & theirs_names)
    assert len(asked) >= 90


# ---- freshness
def test_a_source_that_states_when_it_ends_is_not_used_after_that_day(db, engine, clock):
    owner = str(db.user("u-fresh"))
    biz = str(db.business(owner, "fresh-hosted"))
    old = b"---\nvalid_until: 2026-03-31\n---\n# Returns\n\n## Return window\n\nItems can be returned within 14 days of delivery.\n"
    sid, done = add(db, engine, biz, owner, "returns_2025.md", old)
    assert db.su("select valid_until::text, valid_from, supersedes from public.knowledge_sources where id = %s", (sid,))[0] == ("2026-03-31", None, None)  # the file's own statement is stored
    clock["day"] = date(2026, 3, 1)
    r = ask(engine, biz, owner, "Within how many days can items be returned?")
    assert r["outcome"] == "grounded" and "14 days" in r["answer"]
    clock["day"] = date(2026, 6, 1)  # the same index, the next months: expired, so not used, and the customer is told why
    r = ask(engine, biz, owner, "Within how many days can items be returned?")
    assert r["outcome"] in ("insufficient", "clarification") and "14 days" not in (r.get("answer") or "") and r["outdated"][0]["source"] == "returns_2025.md"
    assert "out of date" in r["message"] and "returns_2025.md" in r["message"]
    assert db.su("select is_current from public.knowledge_units where business_id = %s", (biz,))[0][0] is False
    clock["day"] = DAY


def test_a_new_source_replaces_the_one_it_names_and_only_once_it_is_in_force(db, engine, clock):
    owner = str(db.user("u-replace"))
    biz = str(db.business(owner, "replace-hosted"))
    add(db, engine, biz, owner, "prices_v1.md", b"# Prices\n\n## Shipping\n\nShipping costs 5 pounds for every order.\n")
    add(db, engine, biz, owner, "prices_v2.md", b"---\nvalid_from: 2026-07-01\nsupersedes: prices_v1.md\n---\n# Prices\n\n## Shipping\n\nShipping costs 7 pounds for every order.\n")
    clock["day"] = date(2026, 6, 15)  # announced, not yet in force: the current price still holds
    r = ask(engine, biz, owner, "How much does shipping cost?")
    assert "5 pounds" in r["answer"] and "7 pounds" not in r["answer"]
    clock["day"] = date(2026, 7, 2)
    r = ask(engine, biz, owner, "How much does shipping cost?")
    assert "7 pounds" in r["answer"] and "5 pounds" not in r["answer"] and r["sources"][0]["source"] == "prices_v2.md"
    units = {u[0]: (u[1], u[2]) for u in db.su("select source_name, is_current, superseded_by from public.knowledge_units where business_id = %s", (biz,))}
    assert units["prices_v1.md"] == (False, "prices_v2.md") and units["prices_v2.md"][0] is True
    clock["day"] = DAY


def test_a_header_that_is_wrong_never_stops_the_text_from_being_read(db, engine):
    owner = str(db.user("u-badheader"))
    biz = str(db.business(owner, "badheader-hosted"))
    sid, done = add(db, engine, biz, owner, "bad_dates.md", b"---\nvalid_from: next spring\nvalid_until: 2020-01-01\n---\n# Notes\n\n## Opening\n\nThe shop opens at 9am.\n")
    assert done["chunks"] == 1 and any("not a date" in w for w in done["warnings"])
    assert db.su("select valid_from, valid_until::text from public.knowledge_sources where id = %s", (sid,))[0] == (None, "2020-01-01")
    sid2, done2 = add(db, engine, biz, owner, "reversed.md", b"---\nvalid_from: 2030-01-01\nvalid_until: 2029-01-01\n---\n# Notes\n\n## Closing\n\nThe shop closes at 6pm.\n")
    assert done2["chunks"] == 1 and any("after it ends" in w for w in done2["warnings"])
    assert db.su("select valid_from, valid_until from public.knowledge_sources where id = %s", (sid2,))[0] == (None, None)


def test_a_person_can_state_an_end_date_and_the_engine_follows_without_reading_again(db, engine, clock):
    owner = str(db.user("u-edit"))
    biz = str(db.business(owner, "edit-hosted"))
    sid, _ = add(db, engine, biz, owner, "offer.md", b"# Offer\n\n## Spring offer\n\nThe spring offer gives 10 percent off all repairs.\n")
    assert "10 percent" in ask(engine, biz, owner, "What does the spring offer give?")["answer"]
    db.su("update public.knowledge_sources set valid_until = '2026-05-31' where id = %s", (sid,))
    r = ask(engine, biz, owner, "What does the spring offer give?")
    assert "10 percent" not in (r.get("answer") or "") and r["outdated"][0]["reason"].startswith("expired on 2026-05-31")


# ---- lifecycle
def test_deleting_a_source_removes_its_chunks_and_vectors_and_the_answers(db, engine):
    owner = str(db.user("u-delete"))
    biz = str(db.business(owner, "delete-hosted"))
    sid, _ = add(db, engine, biz, owner, "parking.md", b"# Visiting\n\n## Parking\n\nParking is free for two hours in the car park behind the shop.\n")
    assert "two hours" in ask(engine, biz, owner, "How long is parking free?")["answer"]
    assert db.su("select count(*) from public.embeddings where business_id = %s", (biz,))[0][0] >= 1
    db.su("delete from public.knowledge_sources where id = %s", (sid,))
    for table in ("document_chunks", "faqs", "embeddings"):
        assert db.su(f"select count(*) from public.{table} where business_id = %s", (biz,))[0][0] == 0, table
    r = ask(engine, biz, owner, "How long is parking free?")
    assert "two hours" not in (r.get("answer") or "") and r["outcome"] in ("insufficient", "clarification")


def test_reading_a_source_again_neither_duplicates_nor_keeps_the_old_reading_current(db, engine):
    owner = str(db.user("u-reread"))
    biz = str(db.business(owner, "reread-hosted"))
    v1 = b"# Shop\n\n## Hours\n\nThe shop opens at 9am.\n\n## Phone\n\nCall 555 0100 to book.\n"
    sid, first = add(db, engine, biz, owner, "shop.md", v1)
    assert engine.ingest(biz, owner, sid) == {"unchanged": True, "version": 1}
    engine.fetch_object[f"{biz}/shop.md"] = v1.replace(b"9am", b"10am")
    second = engine.ingest(biz, owner, sid)
    assert second["version"] == 2 and second["chunks_added"] == 1 and second["chunks_removed"] == 1 and second["chunks_unchanged"] == 1
    units = db.su("select content, version from public.knowledge_units where business_id = %s order by content", (biz,))
    assert len(units) == 2 and {u[1] for u in units} == {2}  # the whole current reading is version 2; nothing of version 1 is current
    assert not any("9am" in u[0] for u in units)
    assert db.su("select count(*) from public.document_chunks where source_id = %s and not active", (sid,))[0][0] == 2  # the old reading is kept, inactive, as history
    r = ask(engine, biz, owner, "When does the shop open?")
    assert "10am" in r["answer"] and "9am" not in r["answer"]
    history = db.su("select version, change_summary ->> 'chunks_added' from public.knowledge_source_versions where source_id = %s order by version", (sid,))
    assert history == [(1, "2"), (2, "1")]


def test_a_source_that_cannot_be_read_does_not_disturb_the_knowledge_of_the_others(db, engine):
    owner = str(db.user("u-broken"))
    biz = str(db.business(owner, "broken-hosted"))
    add(db, engine, biz, owner, "good.md", b"# Good\n\n## Fact\n\nThe warranty lasts 2 years.\n")
    with pytest.raises(svc.ServiceError):
        add(db, engine, biz, owner, "broken.xlsx", b"PK\x03\x04 not really a workbook")
    assert "2 years" in ask(engine, biz, owner, "How long does the warranty last?")["answer"]
    assert db.su("select status from public.knowledge_sources where business_id = %s and name = 'broken.xlsx'", (biz,))[0][0] == "failed"
    assert db.su("select count(*) from public.knowledge_units where business_id = %s", (biz,))[0][0] == 1


# ---- structured reasoning through the database (Phase G): the rows of tables are stored as data and compared
REASONING = ROOT / "examples" / "reasoning"


@pytest.fixture(scope="module")
def beacon(db, engine):
    owner = str(db.user("u-beacon"))
    biz = str(db.business(owner, "beacon-hosted"))
    for p in sorted((REASONING / "knowledge").iterdir()):
        if p.suffix.lower() in UB.sources.SUPPORTED:
            add(db, engine, biz, owner, p.name, p.read_bytes(), "faq_table" if p.name == "faq.csv" else "document")
    return type("B", (), {"biz": biz, "owner": owner})


def test_the_rows_of_a_table_are_stored_as_data_with_their_original_headers(beacon, db):
    loc = db.su("select location -> 'record' from public.document_chunks where business_id = %s and location -> 'record' ->> 'entity' = 'Growth' and active", (beacon.biz,))[0][0]
    assert loc["noun"] == "plan" and [f["header"] for f in loc["fields"]][:3] == ["Monthly price (USD)", "Agents", "Storage (GB)"] and loc["fields"][0]["text"] == "79 USD"
    empty = db.su("select location -> 'record' -> 'fields' from public.document_chunks where business_id = %s and location -> 'record' ->> 'entity' = 'Free' and active", (beacon.biz,))[0][0]
    assert any(f["header"].startswith("Extra agent") and f["text"] == "" for f in empty)  # an empty cell is kept as empty, not dropped


def test_questions_that_compare_rows_are_answered_from_the_stored_rows_with_references(beacon, engine, db):
    r = ask(engine, beacon.biz, beacon.owner, "Which plan is the cheapest?")
    assert r["outcome"] == "grounded" and r["answer"].startswith("Free has the lowest Monthly price: 0 USD. [1]") and r["sources"][0]["db_id"]
    owner = db.su("select business_id from public.document_chunks where id = %s", (r["sources"][0]["db_id"],))
    assert [str(o[0]) for o in owner] == [beacon.biz]
    r = ask(engine, beacon.biz, beacon.owner, "Which plans include WhatsApp?")
    assert all(x in r["answer"] for x in ("Growth", "Scale", "Enterprise")) and "give different plans" in r["answer"]
    assert "87 USD" in ask(engine, beacon.biz, beacon.owner, "How much would 3 Team plans cost per month?")["answer"]
    assert ask(engine, beacon.biz, beacon.owner, "Can I have 10 agents on the Team plan?")["answer"].startswith("No")
    r = ask(engine, beacon.biz, beacon.owner, "Which plans have the mobile app?")
    assert r["outcome"] == "insufficient" and "does not name plans" in r["message"]


def test_the_hosted_service_answers_the_reasoning_sets_as_the_local_benchmark_does(beacon, engine):
    for name in ("dev", "regression"):
        rows = [dict(r) for r in csv.DictReader(open(REASONING / f"{name}.csv", encoding="utf-8"))]
        wrong = []
        for row in rows:
            r = ask(engine, beacon.biz, beacon.owner, row["question"])
            if not UB.judge(row, r)["correct"]:
                wrong.append(row["id"])
        assert len(wrong) / len(rows) <= 0.06, (name, wrong)  # the same code, through stored passages and vectors, with the day set by the clock


def test_what_is_asked_about_a_later_day_is_answered_for_that_day_through_the_database(beacon, engine, clock):
    now = ask(engine, beacon.biz, beacon.owner, "When is support open?")
    later = ask(engine, beacon.biz, beacon.owner, "When is support open from 1 July 2026?")
    assert "Monday to Friday" in now["answer"] and "Saturday" not in now["answer"] and "Saturday" in later["answer"]
