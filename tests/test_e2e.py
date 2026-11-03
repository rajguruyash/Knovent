"""One business, from sign-up to a changed answer, in the order a customer meets it, on the real schema, the real embedding model, and stand-in providers over real HTTP. Nothing is mocked inside the
service: invitations are the database's own functions, ingestion reads a real file, every answer goes through the pipeline, and what each provider received is read from the provider's side.

The story: an owner makes a business and invites an editor; knowledge is uploaded; questions are answered with citations (and a question the knowledge cannot answer is not answered); the owner moves
to their own provider, agrees, and sees a model-worded answer; the provider fails and the answer is still honest about who produced it; the agreement is withdrawn and nothing more is sent; the
business moves to its own machine; usage and history record all of it; the knowledge changes and so does the answer; a source is removed and its facts go with it; and a stranger saw none of it."""
import base64
import json
import re
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("cryptography")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_provider import FakeProvider  # noqa: E402
from pgdb import make_db  # noqa: E402

from faqengine.hosted import service as svc  # noqa: E402
from faqengine.hosted.secrets import SecretBox  # noqa: E402
from faqengine.hosted.store import Store  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
pytestmark = pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
FAQ = (ROOT / "examples" / "bike-shop" / "faq.csv").read_bytes()
KEY = "sk-e2e-customer-key-424242"
HOURS = "What are your opening hours?"


def restate_first_evidence(body):
    """A well-behaved model: it answers with the first numbered evidence item, cited."""
    text = " ".join(m.get("content", "") for m in body.get("messages", []))
    m = re.search(r"\[(E\d+)\] \([^)]*\) (.*)", text)
    if not m:
        return json.dumps({"verdict": "insufficient", "claims": [], "missing": ["everything"]})
    return json.dumps({"verdict": "answered", "claims": [{"text": re.split(r"(?<=[.!?])\s", m.group(2).strip())[0], "evidence": [m.group(1)]}], "missing": []})


class Files(dict):
    def __call__(self, path):
        return self[path]


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def provider():
    p = FakeProvider(key=KEY, reply=restate_first_evidence)
    yield p
    p.close()


@pytest.fixture(scope="module")
def engine(db):
    box = SecretBox({1: base64.b64decode(SecretBox.generate())})
    return svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), allow_private_llm=True, rate_per_minute=10_000, ai_modes={"byok", "local"}, inference_per_minute=100, pending_ttl=60.0, fast_path=False)  # these tests are about the MODEL path (consent, routing, limits, metering, verification): the deterministic fast path has its own tests (test_fast_path.py)


def completions(p):
    return [r for r in p.requests if r["method"] == "POST" and r["path"].endswith("/chat/completions")]


def upload(db, engine, biz, name, data, kind="faq_table"):
    engine.fetch_object[f"{biz}/{name}"] = data
    return str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, %s, %s, %s) returning id", (biz, name, kind, f"{biz}/{name}"))[0][0])


def test_a_business_from_sign_up_to_a_changed_answer(db, engine, provider, verified_providers):
    # ---- people and the business
    owner, editor, viewer, stranger = (str(db.user(n)) for n in ("e2e-owner", "e2e-editor", "e2e-viewer", "e2e-stranger"))
    biz = str(db.business(owner, "e2e-bikes"))
    assert db.run(owner, "select role from public.business_members where business_id = %s and user_id = %s", (biz, owner))[0][0] == "owner"

    # ---- an invitation is the only way in: for the invited address, once
    mail = lambda u: db.su("select email from auth.users where id = %s", (u,))[0][0]  # noqa: E731
    token = db.run(owner, "select public.create_invitation(%s, %s, 'editor')", (biz, mail(editor)))[0][0]
    with pytest.raises(Exception):
        db.run(stranger, "select public.accept_invitation(%s)", (token,))  # someone else holding the secret
    assert str(db.run(editor, "select public.accept_invitation(%s)", (token,))[0][0]) == biz
    with pytest.raises(Exception):
        db.run(editor, "select public.accept_invitation(%s)", (token,))  # spent
    db.member(biz, viewer, "viewer")
    for call in (lambda: engine.respond(biz, stranger, HOURS), lambda: engine.history_get(biz, stranger), lambda: engine.ai_settings_get(biz, stranger)):
        with pytest.raises(svc.ServiceError) as e:
            call()
        assert e.value.status == 403  # a person outside the business gets nothing, not even "no such business"

    # ---- knowledge: a viewer cannot add it, an editor can, and the progress is on the source
    sid = upload(db, engine, biz, "faq.csv", FAQ)
    with pytest.raises(svc.ServiceError) as e:
        engine.ingest(biz, viewer, sid)
    assert e.value.status == 403
    done = engine.ingest(biz, editor, sid)
    assert done["faqs"] > 5 and done["unchanged"] is False and engine.ingest(biz, editor, sid)["unchanged"] is True  # asking again changes nothing
    assert db.su("select status from public.knowledge_sources where id = %s", (sid,))[0][0] == "ready"

    # ---- a question is answered from the knowledge, cited, with nothing sent anywhere
    first = engine.respond(biz, viewer, HOURS)["result"]
    ex = first["execution"]
    assert first["answer"].startswith("We are open") and first["sources"] and ex["generated_by"] == "deterministic" and ex["mode"] == "deterministic" and ex["inference_attempted"] is False
    assert completions(provider) == []
    off = engine.respond(biz, viewer, "Do you rent submarines for deep sea diving?")["result"]
    assert off["answer"] is None or off["verified"] is False  # a question the knowledge cannot answer is not answered

    # ---- the owner moves to their own provider: the key is stored encrypted and never returned
    saved = engine.llm_save(biz, owner, {"provider": "openai_compatible", "model": "chat-small", "base_url": provider.url(), "api_key": KEY, "enabled": True})
    assert KEY not in json.dumps(saved) and "api_key" not in json.dumps(engine.llm_public(biz, owner)).replace("has_key", "")
    stored = db.su("select encrypted_api_key from public.llm_provider_secrets where business_id = %s", (biz,))[0][0]
    assert KEY.encode() not in bytes(stored)
    engine.ai_settings_save(biz, owner, {"mode": "byok"})
    provider.requests.clear()
    ex = engine.respond(biz, viewer, HOURS)["result"]["execution"]
    assert ex["mode"] == "byok" and ex["failure_kind"] == "consent:missing_or_revoked" and ex["generated_by"] == "deterministic_no_consent"  # chosen, but not agreed: nothing was sent
    assert completions(provider) == []

    # ---- the agreement is explicit, for this provider, for this text, and the answer then says a model worded it
    with pytest.raises(svc.ServiceError):
        engine.consent_grant(biz, editor, "openai_compatible", svc.disclosure.current_version("openai_compatible"))  # an editor cannot agree for the business
    engine.consent_grant(biz, owner, "openai_compatible", svc.disclosure.current_version("openai_compatible"))
    r = engine.respond(biz, viewer, HOURS)["result"]
    ex = r["execution"]
    assert ex["generated_by"] == "model" and ex["mode"] == "byok" and ex["provider"] == "openai_compatible" and ex["model"] == "chat-small" and r["verified"] is True and r["answer"].startswith("We are open")
    sent = completions(provider)
    assert len(sent) == 1 and sent[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert "submarine" not in json.dumps(sent[0]["body"]).lower()  # only this question's evidence went, not the rest of the knowledge

    # ---- the provider fails: the answer is the deterministic one, labelled as that, with the reason; nothing else is tried
    provider.status = 500
    ex = engine.respond(biz, viewer, HOURS)["result"]["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"].startswith("provider:") and ex["provider"] == "openai_compatible"
    provider.status = None

    # ---- the agreement is withdrawn: the very next question sends nothing
    engine.consent_revoke(biz, owner, "openai_compatible")
    provider.requests.clear()
    ex = engine.respond(biz, viewer, HOURS)["result"]["execution"]
    assert ex["generated_by"] == "deterministic_no_consent" and completions(provider) == []

    # ---- the business's own machine: the evidence goes to the browser's companion and to no provider
    comp = engine.companion_register(biz, owner, {"label": "shop laptop", "platform": "macOS 15 arm64", "runtime": "ollama", "model": "qwen3:4b"})
    engine.ai_settings_save(biz, owner, {"mode": "local"})
    prep = engine.local_prepare(biz, viewer, HOURS)
    assert prep["done"] is False and prep["evidence_ids"] and completions(provider) == []
    sentence = re.search(r"\[E1\] \([^)]*\) (.*)", prep["prompt"]["user"]).group(1).split(". ")[0].rstrip(".") + "."
    out = engine.local_complete(biz, viewer, prep["request_id"], json.dumps({"verdict": "answered", "claims": [{"text": sentence, "evidence": ["E1"]}], "missing": []}), None, 90, 30)["result"]
    assert out["execution"]["generated_by"] == "model" and out["execution"]["mode"] == "local" and out["verified"] is True and completions(provider) == []
    assert comp and engine.companions_get(biz, viewer)["companions"]

    # ---- usage and history record what happened, by mode, and say who produced each answer
    usage = engine.usage_get(biz, owner)["summary"]["used"]
    assert usage["byok"]["requests"] >= 2 and usage["local"]["requests"] >= 1  # (the failed call is counted too: it was made)
    items = engine.history_get(biz, owner, 20)["items"]
    produced = [(i["execution"]["mode"], i["execution"]["generated_by"]) for i in items]
    assert ("byok", "model") in produced and ("local", "model") in produced and ("deterministic", "deterministic") in produced and ("byok", "deterministic_fallback") in produced

    # ---- the knowledge changes and the answer follows (re-index), then a source is removed and its facts go with it
    engine.ai_settings_save(biz, owner, {"mode": "deterministic"})
    changed = FAQ.replace(b"We are open", b"We are now open")
    assert changed != FAQ
    engine.fetch_object[f"{biz}/faq.csv"] = changed
    assert engine.ingest(biz, editor, sid)["unchanged"] is False
    assert engine.respond(biz, viewer, HOURS)["result"]["answer"].startswith("We are now open")
    db.run(owner, "delete from public.knowledge_sources where id = %s", (sid,))
    assert db.su("select count(*) from public.document_chunks where business_id = %s", (biz,))[0][0] == 0 and db.su("select count(*) from public.faqs where business_id = %s", (biz,))[0][0] == 0
    gone = engine.respond(biz, viewer, HOURS)["result"]
    assert gone["answer"] is None or "open" not in (gone["answer"] or "").lower()

    # ---- and the stranger, throughout, saw none of it
    assert db.run(stranger, "select count(*) from public.messages")[0][0] == 0
    assert db.run(stranger, "select count(*) from public.usage_records")[0][0] == 0
    assert db.run(stranger, "select count(*) from public.ai_inference_consents")[0][0] == 0
