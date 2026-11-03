"""What a person approves while testing goes back into retrieval, on the real schema (PostgreSQL with pgvector) and the real embedding model.

Not a training run: the question is stored as one more way of asking its entry, and the index (rebuilt when the learned questions change) searches it from the next question on."""
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("psycopg")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgdb import make_db  # noqa: E402

from faqengine.hosted import service as svc  # noqa: E402
from faqengine.hosted.store import Store  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
pytestmark = pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
FAQ_CSV = (ROOT / "examples" / "bike-shop" / "faq.csv").read_bytes()
ODD = "zebra crossing banana split"  # nothing in a bike shop's FAQ is about this: only an approval can make it find an entry


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def world(db):
    w = type("W", (), {})()
    w.owner, w.editor, w.viewer, w.other = (str(db.user(n)) for n in ("f-owner", "f-editor", "f-viewer", "f-other"))
    w.A, w.B = str(db.business(w.owner, "bikes-feedback")), str(db.business(w.other, "groomer-feedback"))
    db.member(w.A, w.editor, "editor")
    db.member(w.A, w.viewer, "viewer")
    files = {f"{w.A}/faq.csv": FAQ_CSV}
    w.engine = svc.HostedEngine(Store(db.dsn), Embedder(MODEL), None, lambda p: files[p], rate_per_minute=10_000, fast_path=False)
    sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, 'faq.csv', 'faq_table', %s) returning id", (w.A, f"{w.A}/faq.csv"))[0][0])
    w.engine.ingest(w.A, w.editor, sid)
    w.hours = str(db.su("select id from public.faqs where business_id = %s and question ilike %s", (w.A, "What are your opening hours%"))[0][0])
    return w


def top(engine, world, question):
    r = engine.respond(world.A, world.viewer, question)
    return r, [s.get("db_id") for s in (r["result"].get("sources") or [])]


def test_an_approved_question_is_found_afterwards_and_forgetting_it_undoes_that(db, world):
    e = world.engine
    _, before = top(e, world, ODD)
    assert world.hours not in before[:1]  # nothing links this question to the opening hours
    out = e.feedback_approve(world.A, world.editor, world.hours, "  " + ODD + "  ")
    assert out["learned"]["question"] == ODD and out["total"] == 1 and out["for_this_entry"] == 1  # whitespace tidied
    r, after = top(e, world, ODD)
    assert after and after[0] == world.hours  # retrieval now goes to the entry the person approved
    assert db.su("select verdict, status from public.feedback where business_id = %s and faq_id = %s", (world.A, world.hours))[0] == ("approve", "accepted")
    listed = e.learned_list(world.A, world.viewer)["learned"]  # anyone in the business may read what was learned
    assert [(x["question"], x["faq_id"]) for x in listed] == [(ODD, world.hours)]
    e.learned_remove(world.A, world.editor, listed[0]["id"])
    _, gone = top(e, world, ODD)
    assert world.hours not in gone[:1] and e.learned_list(world.A, world.viewer)["learned"] == []


def test_who_may_approve_and_the_refusals(db, world):
    e = world.engine
    for who, status in ((world.viewer, 403), (world.other, 403)):  # a viewer reads; a person of another business has no access at all
        with pytest.raises(svc.ServiceError) as x:
            e.feedback_approve(world.A, who, world.hours, "can I come in on Sunday")
        assert x.value.status == status
    with pytest.raises(svc.ServiceError) as x:
        e.feedback_approve(world.B, world.other, world.hours, "can I come in on Sunday")  # B's own person, A's entry: not B's knowledge
    assert x.value.status == 404
    for bad, status in (("not-a-uuid", 400),):
        with pytest.raises(svc.ServiceError) as x:
            e.feedback_approve(world.A, world.editor, bad, "q")
        assert x.value.status == status
    for q in ("", "x" * 301):
        with pytest.raises(svc.ServiceError) as x:
            e.feedback_approve(world.A, world.editor, world.hours, q)
        assert x.value.status == 400
    with pytest.raises(svc.ServiceError) as x:
        e.feedback_approve(world.A, world.editor, str(uuid.uuid4()), "anything at all")
    assert x.value.status == 404
    with pytest.raises(svc.ServiceError) as x:  # already an FAQ question: it is knowledge, not a new way of asking
        e.feedback_approve(world.A, world.editor, world.hours, "What are your opening hours?")
    assert x.value.status == 409
    e.feedback_approve(world.A, world.editor, world.hours, "is the shop open late on fridays")
    with pytest.raises(svc.ServiceError) as x:
        e.feedback_approve(world.A, world.editor, world.hours, "Is the shop open late on Fridays?")  # the same question, differently written
    assert x.value.status == 409
    other = e.learned_list(world.A, world.owner)["learned"][0]["id"]
    with pytest.raises(svc.ServiceError) as x:
        e.learned_remove(world.A, world.viewer, other)
    assert x.value.status == 403
    with pytest.raises(svc.ServiceError) as x:
        e.learned_remove(world.B, world.other, other)  # another business's learned question cannot be reached by its id
    assert x.value.status == 404
    assert len(e.learned_list(world.A, world.owner)["learned"]) == 1


def test_a_question_from_a_test_set_never_becomes_knowledge(db, world):
    e = world.engine
    ds = str(db.su("insert into public.evaluation_datasets (business_id, name, role) values (%s, 'held out', 'development') returning id", (world.A,))[0][0])
    db.su("insert into public.evaluation_questions (business_id, dataset_id, question, expected_faq_id, expected_outcome, group_key) values (%s, %s, %s, %s, 'faq', 'g1') returning id", (world.A, ds, "do you open on bank holidays", world.hours))
    with pytest.raises(svc.ServiceError) as x:
        e.feedback_approve(world.A, world.editor, world.hours, "do you open on bank holidays")
    assert x.value.status == 409


def test_the_http_routes_exist_and_need_the_shared_secret(world):
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), svc.make_handler(world.engine, "s" * 30))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def post(path, body, secret="s" * 30):
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())
        assert post("/v1/feedback/learned", {"business_id": world.A, "user_id": world.viewer}, secret="wrong" * 8)[0] == 401
        status, body = post("/v1/feedback/learned", {"business_id": world.A, "user_id": world.viewer})
        assert status == 200 and len(body["learned"]) == 1
        assert post("/v1/feedback/approve", {"business_id": world.A, "user_id": world.viewer, "faq_id": world.hours, "question": "q"})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
