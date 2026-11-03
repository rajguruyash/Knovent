"""The deterministic fast path IN THE HOSTED SERVICE: the real schema, the real embedding model, a stand-in provider over real HTTP (so "nothing was sent" is read from the provider's side).

What is proved: an exact FAQ answer is given without the provider (and without consent, because nothing is sent), while a question that needs a model still goes through consent, limits and routing exactly
as before; a business never gets another business's answer; an expired entry is not served just because it was an exact match; the local-first two-phase flow does not ask the person's computer for
an answer that already exists; no provider call is metered for a fast answer; the switch turns it off; and the audit record says how each answer was made. No customer setting, consent or
fallback permission is touched."""
import base64
import json
import re
import sys
from datetime import date
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
BYOK_KEY = "sk-fastpath-key-515151"
BIKE = (ROOT / "examples" / "bike-shop" / "faq.csv").read_bytes()
ACME = (ROOT / "examples" / "unified" / "knowledge" / "faq.csv").read_bytes()
HOURS = "What are your opening hours?"
NEEDS_A_MODEL = "How long will fixing my bike take?"  # not one of the FAQ's own phrasings: matched by meaning, so the model composes


def claims_reply(body):
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
    p = FakeProvider(key=BYOK_KEY, reply=claims_reply)
    yield p
    p.close()


@pytest.fixture(scope="module")
def engine(db):
    box = SecretBox({1: base64.b64decode(SecretBox.generate())})
    return svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), allow_private_llm=True, rate_per_minute=10_000, ai_modes={"byok", "local"}, inference_per_minute=1000, pending_ttl=60.0)  # (fast path: the default, on)


@pytest.fixture(scope="module")
def world(db, engine):
    w = type("W", (), {})()
    w.owner, w.viewer, w.other = (str(db.user(n)) for n in ("fp-owner", "fp-viewer", "fp-other"))
    w.A, w.B = str(db.business(w.owner, "fastpath-a")), str(db.business(w.other, "fastpath-b"))
    db.member(w.A, w.viewer, "viewer")
    for biz, who, data in ((w.A, w.owner, BIKE), (w.B, w.other, ACME)):
        engine.fetch_object[f"{biz}/faq.csv"] = data
        sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, 'faq.csv', 'faq_table', %s) returning id", (biz, f"{biz}/faq.csv"))[0][0])
        engine.ingest(biz, who, sid)
        if biz == w.A:
            w.sid = sid
    return w


@pytest.fixture(autouse=True)
def _providers(verified_providers):
    return None


@pytest.fixture(autouse=True)
def clean(db, engine, world, provider):
    yield
    for b, u in ((world.A, world.owner), (world.B, world.other)):
        engine.store.save_ai_settings(b, u, "deterministic", None, None, "deterministic")
        db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where business_id = %s and status = 'granted'", (b,))
        for table in ("llm_provider_secrets", "llm_provider_configs"):
            db.su(f"delete from public.{table} where business_id = %s", (b,))
        db.su("delete from public.usage_records where business_id = %s and kind = 'llm_call'", (b,))
        db.su("update public.local_companions set revoked_at = now() where business_id = %s and revoked_at is null", (b,))
    db.su("update public.knowledge_sources set valid_until = null where id = %s", (world.sid,))
    engine.fast_path = True
    engine.today = date.today
    engine._pending.clear()
    provider.requests.clear()
    provider.status = None


def calls(p):
    return [r for r in p.requests if r["method"] == "POST" and r["path"].endswith("/chat/completions")]


def use_own_provider(engine, db, biz, owner, provider, consent=True):
    db.su("insert into public.llm_provider_configs (business_id, provider, model, base_url, enabled, has_key) values (%s, 'openai_compatible', 'chat-small', %s, true, true)", (biz, provider.url()))
    row = engine.secrets.encrypt(BYOK_KEY, biz)
    db.su("insert into public.llm_provider_secrets (business_id, encrypted_api_key, key_version) values (%s, %s, %s)", (biz, row[0], row[1]))
    if consent:
        engine.consent_grant(biz, owner, "openai_compatible", svc.disclosure.current_version("openai_compatible"))
    engine.ai_settings_save(biz, owner, {"policy": "byok_preferred"})


def ask(engine, biz, user, q):
    return engine.respond(biz, user, q)["result"]


# ================================================================== the model is skipped only for what is complete, and only that
def test_an_exact_faq_answer_never_reaches_the_provider_and_is_not_metered_as_a_model_call(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider)
    r = ask(engine, world.A, world.viewer, HOURS)
    ex = r["execution"]
    assert calls(provider) == [] and r["answer"].startswith("We are open") and r["verified"] is True and r["outcome"] == "faq"
    assert (ex["answer_path"], ex["fast_path_reason"], ex["model_invoked"], ex["provider_steps_tried"], ex["generated_by"]) == ("fast_path", "exact_faq_direct", False, [], "deterministic")
    assert [a["outcome"] for a in ex["attempts"]] == ["no_model_needed"] and ex["evidence_ids"] and ex["verification"]["passed"] is True
    assert db.su("select count(*) from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,))[0][0] == 0  # nothing to meter: nothing was asked
    assert db.su("select generated_by, mode from public.usage_records where business_id = %s and kind = 'question' order by created_at desc limit 1", (world.A,))[0] == ("deterministic", "byok")
    assert engine.tele.counters["answer_path_fast_path"] >= 1


def test_a_question_that_needs_a_model_still_goes_through_the_provider_and_is_metered_as_before(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider)
    r = ask(engine, world.A, world.viewer, NEEDS_A_MODEL)
    ex = r["execution"]
    assert len(calls(provider)) == 1 and ex["answer_path"] == "model" and ex["model_invoked"] is True and ex["provider_steps_tried"] == ["byok"] and ex["generated_by"] == "model"
    assert ex["fast_path_reason"].startswith("not_eligible:") and r["verified"] is True
    assert db.su("select tier, status from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,)) == [("byok", "ok")]


def test_consent_is_still_enforced_for_what_needs_a_model_and_not_needed_for_what_does_not(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider, consent=False)  # chosen, but the business has not agreed to send data to the provider
    exact = ask(engine, world.A, world.viewer, HOURS)
    assert exact["execution"]["answer_path"] == "fast_path" and not exact["execution"].get("failure_kind") and calls(provider) == []  # nothing is sent, so nothing needs an agreement
    needs = ask(engine, world.A, world.viewer, NEEDS_A_MODEL)
    assert needs["execution"]["generated_by"] == "deterministic_no_consent" and needs["execution"]["failure_kind"] == "consent:missing_or_revoked" and calls(provider) == []  # unchanged
    engine.consent_grant(world.A, world.owner, "openai_compatible", svc.disclosure.current_version("openai_compatible"))
    assert ask(engine, world.A, world.viewer, NEEDS_A_MODEL)["execution"]["generated_by"] == "model" and len(calls(provider)) == 1
    db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where business_id = %s and status = 'granted'", (world.A,))
    assert ask(engine, world.A, world.viewer, NEEDS_A_MODEL)["execution"]["generated_by"] == "deterministic_no_consent" and len(calls(provider)) == 1  # withdrawal still stops the very next call


def test_no_setting_changed_hands_with_the_fast_path(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider)
    before = (engine.store.ai_settings(world.A), engine.store.llm_config(world.A))
    for q in (HOURS, NEEDS_A_MODEL, HOURS):
        ask(engine, world.A, world.viewer, q)
    assert (engine.store.ai_settings(world.A), engine.store.llm_config(world.A)) == before  # mode, fallbacks, spending cap, provider and model are as the customer left them


def test_with_the_operators_switch_off_an_exact_faq_answer_goes_to_the_provider_as_before(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider)
    engine.fast_path = False
    r = ask(engine, world.A, world.viewer, HOURS)
    assert len(calls(provider)) == 1 and r["execution"]["answer_path"] == "model" and r["execution"]["fast_path_reason"] == "disabled"


def test_a_business_with_no_ai_is_unchanged(engine, world, provider):
    r = ask(engine, world.A, world.viewer, HOURS)
    assert r["outcome"] == "faq" and r["answer"].startswith("We are open") and calls(provider) == []  # the deterministic business never had a model to skip


# ================================================================== other businesses, and old information
def test_one_business_never_gets_anothers_answer_through_the_fast_path(engine, db, world, provider):
    for biz, owner in ((world.A, world.owner), (world.B, world.other)):
        use_own_provider(engine, db, biz, owner, provider)
    mine = ask(engine, world.A, world.viewer, HOURS)
    theirs = ask(engine, world.B, world.other, HOURS)  # B's knowledge is a software company's FAQ: it has no opening hours
    assert mine["execution"]["answer_path"] == "fast_path" and "Monday to Saturday" in mine["answer"]
    assert theirs["outcome"] == "insufficient" and theirs["answer"] is None and theirs["execution"]["answer_path"] != "fast_path" and "Monday" not in json.dumps(theirs)
    trial_b = ask(engine, world.B, world.other, "Do you offer a free trial?")
    trial_a = ask(engine, world.A, world.viewer, "Do you offer a free trial?")  # (A is a bike shop)
    assert trial_b["execution"]["answer_path"] == "fast_path" and "14 day free trial" in trial_b["answer"]
    assert trial_a["outcome"] == "insufficient" and trial_a["execution"]["answer_path"] != "fast_path" and "14 day" not in json.dumps(trial_a)
    assert all("faq.csv" in s["source"] for s in trial_b["sources"]) and {s["db_id"] for s in trial_b["sources"]}.isdisjoint({s["db_id"] for s in mine["sources"]})
    assert calls(provider) == []  # and nothing of either business went to the provider for these four questions


def test_an_expired_entry_is_not_served_just_because_the_question_matched_it_exactly(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider)
    assert ask(engine, world.A, world.viewer, HOURS)["execution"]["answer_path"] == "fast_path"
    db.su("update public.knowledge_sources set valid_until = '2026-01-01' where id = %s", (world.sid,))
    engine.today = lambda: date(2026, 6, 1)  # the source's own end date has passed
    r = ask(engine, world.A, world.viewer, HOURS)
    ex = r["execution"]
    assert r["outcome"] == "insufficient" and r["answer"] is None and ex["answer_path"] == "abstained" and calls(provider) == []  # the exact match is not used; the customer is told, not guessed at
    db.su("update public.knowledge_sources set valid_until = '2099-01-01' where id = %s", (world.sid,))
    assert ask(engine, world.A, world.viewer, HOURS)["execution"]["answer_path"] == "fast_path"  # current again: served


def test_missing_information_stays_an_abstention_and_no_model_is_asked_to_invent_it(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider)
    r = ask(engine, world.A, world.viewer, "Do you rent submarines for deep sea diving?")
    ex = r["execution"]
    assert r["outcome"] == "insufficient" and r["answer"] is None and ex["answer_path"] == "abstained" and ex["model_invoked"] is False and calls(provider) == []


# ================================================================== local first
def pair(engine, world):
    engine.companion_register(world.A, world.owner, {"label": "shop laptop", "platform": "macOS arm64", "runtime": "ollama", "model": "llama3.2:1b", "fit": "comfortable"})
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_only"})


def test_the_two_phase_local_flow_does_not_ask_the_persons_computer_for_an_answer_that_exists(engine, db, world, provider):
    pair(engine, world)
    done = engine.local_prepare(world.A, world.viewer, HOURS)
    ex = done["result"]["execution"]
    assert done["done"] is True and "request_id" not in done and done["result"]["answer"].startswith("We are open")
    assert (ex["answer_path"], ex["fast_path_reason"], ex["model_invoked"], ex["provider_steps_tried"], ex["tier"]) == ("fast_path", "exact_faq_direct", False, [], "local")
    assert [a["outcome"] for a in ex["attempts"]] == ["no_model_needed"] and not engine._pending  # nothing is waiting for a reply
    needs = engine.local_prepare(world.A, world.viewer, NEEDS_A_MODEL)
    assert needs["done"] is False and needs["request_id"] and needs["evidence_ids"]  # a question that needs the model still goes to the person's computer
    assert db.su("select count(*) from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,))[0][0] == 0
    assert calls(provider) == []


def test_a_plain_request_for_a_local_first_business_returns_the_complete_answer_and_no_computer_hint(engine, world):
    pair(engine, world)
    ex = ask(engine, world.A, world.viewer, HOURS)["execution"]
    assert ex["answer_path"] == "fast_path" and "local" not in ex and [a["outcome"] for a in ex["attempts"]] == ["no_model_needed"]  # (nothing is left for the computer to do)
    ex2 = ask(engine, world.A, world.viewer, NEEDS_A_MODEL)["execution"]
    assert ex2["failure_kind"] == "local:client_inference_required" and ex2["local"]["available"] is True  # as before


def test_the_answer_path_is_in_the_history(engine, db, world, provider):
    use_own_provider(engine, db, world.A, world.owner, provider)
    ask(engine, world.A, world.viewer, HOURS)
    ask(engine, world.A, world.viewer, NEEDS_A_MODEL)
    items = engine.history_get(world.A, world.owner, 5)["items"]
    paths = [i["execution"].get("answer_path") for i in items[:2]]
    assert paths == ["model", "fast_path"] and items[1]["execution"]["fast_path_reason"] == "exact_faq_direct"
