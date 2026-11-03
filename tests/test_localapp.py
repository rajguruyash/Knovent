"""The one-command app's engine: real files in, real MiniLM search, cited answers out, and an honest "I don't know". Also that its internal service refuses a caller without the run's secret."""
import base64
import json
import urllib.error
import urllib.request

import pytest

from faqengine.localapp.engine import EngineError, LocalEngine
from faqengine.localapp.server import serve

BID, SID = "b0000000-1111", "s0000000-2222"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    e = LocalEngine(tmp_path_factory.mktemp("knovent"))
    e.ingest(BID, SID, "faq.csv", open("examples/bike-shop/faq.csv", "rb").read())
    return e


def test_an_uploaded_faq_answers_exactly_and_cites_its_source(engine):
    r = engine.respond(BID, "What are your opening hours?")
    assert r["outcome"] == "faq" and "9am to 6pm" in r["answer"] and r["sources"]


def test_a_question_the_knowledge_cannot_answer_is_not_answered(engine):
    r = engine.respond(BID, "do you sell tractors")
    assert r["outcome"] == "insufficient" and not r.get("answer")


def test_another_business_sees_nothing(engine):
    r = engine.respond("c0000000-3333", "What are your opening hours?")
    assert r["outcome"] != "faq" and not r.get("answer")


def test_removing_a_source_removes_its_answers(tmp_path):
    e = LocalEngine(tmp_path)
    e.ingest(BID, SID, "faq.csv", open("examples/bike-shop/faq.csv", "rb").read())
    assert e.respond(BID, "What are your opening hours?")["outcome"] == "faq"
    e.remove(BID, SID)
    assert e.respond(BID, "What are your opening hours?")["outcome"] == "insufficient"


def test_a_file_of_secrets_is_refused(engine):
    with pytest.raises(EngineError) as x:
        engine.ingest(BID, "s0000000-9999", ".env", b"KEY=abc")
    assert x.value.status == 422


def test_the_internal_service_needs_this_runs_secret(engine):
    httpd = serve(engine, "right-secret", 0)
    import threading
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/respond"
    body = json.dumps({"business_id": BID, "query": "What are your opening hours?"}).encode()
    with pytest.raises(urllib.error.HTTPError) as x:
        urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST", headers={"X-Knovent-Secret": "wrong"}))
    assert x.value.code == 403
    ok = urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST", headers={"X-Knovent-Secret": "right-secret"}))
    assert json.loads(ok.read())["outcome"] == "faq"
    httpd.shutdown()
    assert base64  # (imported for symmetry with the website's ingest call)


def test_chat_style_keeps_the_engines_wording_when_the_check_fails(engine, monkeypatch):
    from faqengine.localapp import engine as eng

    monkeypatch.setattr(eng, "_combine", lambda *a, **k: {"text": None, "failure": "failed_verification", "capability": "grounded", "verification": None})
    r = engine.respond(BID, "What are your opening hours?", "any-model", "chat")
    assert "9am to 6pm" in r["answer"] and not r["llm"]["used"] and r["execution"]["answer_path"] == "deterministic_fallback" and r["fallback"]["reason"] == "failed_verification"


def test_a_markdown_document_written_as_question_and_answer_blocks_is_read_as_a_faq(tmp_path):
    from faqengine.sources import load_sources

    md = tmp_path / "02_FAQ.md"
    md.write_text("# Shop FAQ\n\nIntroduction that is not a question.\n\n## Returns\n**Q: What is the return window?**  \nYou have 365 days to return eligible items.\n\n**Q: Can opened items be returned?**  \nYes, if they are complete and unused.\n\n## Delivery\n**Q: Can I choose a delivery time?**  \nNo, carriers give an expected arrival.\n")
    kb = load_sources([md])
    faqs = [e for e in kb.entries if e.kind == "faq"]
    assert [e.questions[0] for e in faqs] == ["What is the return window?", "Can opened items be returned?", "Can I choose a delivery time?"]
    assert faqs[0].answer.startswith("You have 365 days") and faqs[0].category == "Returns" and faqs[2].category == "Delivery"
    assert any(e.kind == "passage" and "Introduction" in e.answer for e in kb.entries)  # the prose is still searchable


def test_a_document_with_a_single_q_line_stays_prose(tmp_path):
    from faqengine.sources import load_sources

    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nQ: this is only one line that looks like a question\nand a paragraph.\n\nMore prose here about the company.\n")
    assert all(e.kind == "passage" for e in load_sources([md]).entries)


def test_a_web_address_on_this_computer_or_its_network_is_never_read(engine):
    for url in ("http://127.0.0.1:8768/", "http://localhost/", "http://192.168.1.1/"):
        with pytest.raises(EngineError) as x:
            engine.ingest_url(BID, "s0000000-7777", url)
        assert x.value.status == 422


def test_chat_style_answers_from_the_retrieved_pieces_and_lets_the_models_refusal_stand(engine, monkeypatch):
    from faqengine.localapp import engine as eng

    seen = {}

    def fake(llm, q, sources, sim=None):
        seen["n"] = len(sources); seen["texts"] = " ".join(s["text"] for s in sources)
        return {"text": "We are open Monday to Saturday, 9am to 6pm [1].", "failure": None, "capability": "grounded", "verification": {"passed": True}}

    monkeypatch.setattr(eng, "_combine", fake)
    r = engine.respond(BID, "What are your opening hours?", "any-model", "chat")
    assert r["llm"]["used"] and r["answer"].startswith("We are open") and seen["n"] >= 1 and "opening hours" in seen["texts"].lower() and r["sources"][0]["ref"] == 1
    monkeypatch.setattr(eng, "_combine", lambda *a, **k: {"text": None, "failure": "declined", "capability": "grounded", "verification": None})
    r = engine.respond(BID, "How long does fixing a repair take?", "any-model", "chat")
    assert r["outcome"] in ("faq", "insufficient")  # an exact FAQ match is never refused away; anything looser becomes an honest refusal
    r = engine.respond(BID, "do you sell tractors", "any-model", "chat")
    assert r["outcome"] == "insufficient" and not r["llm"]["used"]


def test_a_reply_with_some_unsupported_sentences_keeps_only_the_supported_ones():
    from faqengine.localapp.engine import _keep_supported

    res = {"text": None, "failure": "failed_verification", "verification": {"passed": False, "claims": [
        {"text": "Yes, you can get a refund if you change your mind.", "status": "rejected"},
        {"text": "You can request a full refund within 14 days of purchase from the Billing page .", "status": "supported"}]}}
    out = _keep_supported(res)
    assert out["text"] == "You can request a full refund within 14 days of purchase from the Billing page." and out["verification"]["claims_removed"] == 1 and not out["failure"]
    none = {"text": None, "failure": "failed_verification", "verification": {"claims": [{"text": "x", "status": "rejected"}]}}
    assert _keep_supported(none)["text"] is None


def test_stitched_supported_sentences_read_cleanly_when_the_opening_one_was_removed():
    from faqengine.localapp.engine import _keep_supported

    res = {"text": None, "failure": "failed_verification", "verification": {"claims": [
        {"text": "Yes, you can.", "status": "rejected", "refs": [1]},
        {"text": "However, the returns policy page   indicates a 365 day return policy", "status": "supported", "refs": [1, 2]},
        {"text": "Bring proof of purchase.", "status": "supported", "refs": []}]}}
    out = _keep_supported(res)["text"]
    assert out == "The returns policy page indicates a 365 day return policy [1] [2]. Bring proof of purchase."
