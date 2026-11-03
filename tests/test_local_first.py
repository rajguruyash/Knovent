"""LOCAL FIRST: the order in which a business's answers may be made, on the real schema with the real embedding model and stand-in providers over real HTTP.

The order is the business's own computer, then (only if the business turned them on) its own provider account, FREE hosted inference and PAID hosted inference. What is proved: each step is used
only when its provider is verified, the business agreed to that provider and its limits allow it; a step that is not permitted is skipped with NOTHING sent (read from the provider's side); a
provider that fails hands over only to a step the business enabled; a model that answered badly does not send the evidence anywhere else; free is tried before paid; paid stops at the business's own
spending cap; every call is metered against its own step; and the answer says which step answered and why the earlier ones did not."""
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
from faqengine.inference import registry  # noqa: E402
from faqengine.inference.resilience import Breakers  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
pytestmark = pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
FAQ_CSV = (ROOT / "examples" / "bike-shop" / "faq.csv").read_bytes()
FREE_KEY, PAID_KEY, BYOK_KEY = "sk-free-tier-key-111111", "sk-paid-tier-key-222222", "sk-customer-key-333333"
HOURS = "What are your opening hours?"


def claims_reply():
    def reply(body):
        text = " ".join(m.get("content", "") for m in body.get("messages", []))
        m = re.search(r"\[(E\d+)\] \([^)]*\) (.*)", text)
        if not m:
            return json.dumps({"verdict": "insufficient", "claims": [], "missing": ["everything"]})
        return json.dumps({"verdict": "answered", "claims": [{"text": re.split(r"(?<=[.!?])\s", m.group(2).strip())[0], "evidence": [m.group(1)]}], "missing": []})
    return reply


class Files(dict):
    def __call__(self, path):
        return self[path]


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def free_server():
    p = FakeProvider(key=FREE_KEY, reply=claims_reply())
    yield p
    p.close()


@pytest.fixture(scope="module")
def paid_server():
    p = FakeProvider(key=PAID_KEY, reply=claims_reply())
    yield p
    p.close()


@pytest.fixture(scope="module")
def byok_server():
    p = FakeProvider(key=BYOK_KEY, reply=claims_reply())
    yield p
    p.close()


@pytest.fixture(scope="module")
def catalogue_file(tmp_path_factory):
    f = tmp_path_factory.mktemp("cat") / "catalogue.json"
    base = {"structured": "unverified", "verified_on": "2026-11-02", "source": "https://example.invalid/models", "platform_offered": True}
    f.write_text(json.dumps({"models": [
        {**base, "provider": "openrouter", "id": "free-small", "label": "Free test model", "tier": "best_effort_free", "price_in_per_m": 0.0, "price_out_per_m": 0.0},
        {**base, "provider": "cloudflare", "id": "paid-small", "label": "Paid test model", "tier": "paid", "price_in_per_m": 1000.0, "price_out_per_m": 2000.0}]}))
    return f


@pytest.fixture(autouse=True)
def _env(verified_providers, catalogue_file, free_server, paid_server, monkeypatch):
    monkeypatch.setenv("MODEL_CATALOGUE_FILE", str(catalogue_file))
    monkeypatch.setitem(registry.PROVIDERS["openrouter"], "base_url", free_server.url())  # the stand-ins play the two platform providers
    monkeypatch.setitem(registry.PROVIDERS["cloudflare"], "base_url", paid_server.url())


@pytest.fixture(scope="module")
def engine(db):
    box = SecretBox({1: SecretBox.generate() and base64.b64decode(SecretBox.generate())})
    platform = registry.PlatformCredentials((("PLATFORM_OPENROUTER_API_KEY", FREE_KEY), ("PLATFORM_CLOUDFLARE_ACCOUNT_ID", "acct"), ("PLATFORM_CLOUDFLARE_API_TOKEN", PAID_KEY)))
    return svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), allow_private_llm=True, rate_per_minute=10_000, ai_modes={"platform", "byok", "local"}, platform=platform,
                            breakers=Breakers(failures=3, cooldown=60), inference_per_minute=1000, pending_ttl=60.0, fast_path=False)  # these tests are about the MODEL path (consent, routing, limits, metering, verification): the deterministic fast path has its own tests (test_fast_path.py)


@pytest.fixture(scope="module")
def world(db, engine):
    w = type("W", (), {})()
    w.owner, w.editor, w.viewer = (str(db.user(n)) for n in ("lf-owner", "lf-editor", "lf-viewer"))
    w.A = str(db.business(w.owner, "localfirst-a"))
    db.member(w.A, w.editor, "editor")
    db.member(w.A, w.viewer, "viewer")
    engine.fetch_object[f"{w.A}/faq.csv"] = FAQ_CSV
    sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, 'faq.csv', 'faq_table', %s) returning id", (w.A, f"{w.A}/faq.csv"))[0][0])
    engine.ingest(w.A, w.owner, sid)
    return w


@pytest.fixture(autouse=True)
def clean(db, engine, world, free_server, paid_server, byok_server):
    yield
    engine.store.save_ai_settings(world.A, world.owner, "deterministic", None, None, "deterministic")
    db.su("update public.ai_inference_settings set paid_cap_usd = null where business_id = %s", (world.A,))
    db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where business_id = %s and status = 'granted'", (world.A,))
    for table in ("business_plans", "llm_provider_secrets", "llm_provider_configs"):
        db.su(f"delete from public.{table} where business_id = %s", (world.A,))
    db.su("delete from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,))
    db.su("update public.local_companions set revoked_at = now() where business_id = %s and revoked_at is null", (world.A,))
    engine.breakers.__init__(failures=3, cooldown=60)
    engine.ai_modes = frozenset({"platform", "byok", "local"})
    engine.router.enabled_modes = engine.ai_modes
    engine._pending.clear()
    for p in (free_server, paid_server, byok_server):
        p.status, p.statuses, p.reply, p.delay, p.reject = None, [], claims_reply(), 0.0, set()
        p.requests.clear()


def calls(server):
    return [r for r in server.requests if r["method"] == "POST" and r["path"].endswith("/chat/completions")]


def agree(engine, w, provider):
    engine.consent_grant(w.A, w.owner, provider, svc.disclosure.current_version(provider))


def withdraw(db, w, provider):
    db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where business_id = %s and provider = %s and status = 'granted'", (w.A, provider))


def plan_row(db, w, free=(100, 100_000), paid=(100, 100_000, 50)):
    db.su("insert into public.business_plans (business_id, plan, free_requests_per_month, free_tokens_per_month, inference_requests_per_month, tokens_per_month, cost_cap_usd_per_month) "
          "values (%s, 'paid', %s, %s, %s, %s, %s)", (w.A, *free, *paid))


def pair(engine, w, fit="comfortable"):
    return engine.companion_register(w.A, w.owner, {"label": "shop laptop", "platform": "macOS arm64", "runtime": "ollama", "model": "qwen3:1.7b", "fit": fit})


def set_up_byok(engine, db, w, server):
    db.su("insert into public.llm_provider_configs (business_id, provider, model, base_url, enabled, has_key) values (%s, 'openai_compatible', 'chat-small', %s, true, true)", (w.A, server.url()))
    row = engine.secrets.encrypt(BYOK_KEY, w.A)
    db.su("insert into public.llm_provider_secrets (business_id, encrypted_api_key, key_version) values (%s, %s, %s)", (w.A, row[0], row[1]))


def ask(engine, w, q=HOURS):
    return engine.respond(w.A, w.viewer, q)["result"]


def ask_local(engine, w, failure=None, text=None):
    """The person's browser asked the service to prepare a local question and then reported what its companion did."""
    prep = engine.local_prepare(w.A, w.viewer, HOURS)
    assert prep["done"] is False
    sentence = re.search(r"\[E1\] \([^)]*\) (.*)", prep["prompt"]["user"]).group(1).split(". ")[0].rstrip(".") + "."
    good = json.dumps({"verdict": "answered", "claims": [{"text": sentence, "evidence": ["E1"]}], "missing": []})
    return engine.local_complete(w.A, w.viewer, prep["request_id"], None if failure else (text or good), failure, 90, 30)["result"]


# ================================================================== what a person can choose, and what it takes
def test_every_follower_is_off_by_default_and_deterministic_only_means_nothing_else(engine, world):
    s = engine.ai_settings_get(world.A, world.owner)
    assert s["settings"]["mode"] == "deterministic" and not any(s["settings"][f] for f in svc.HostedEngine.FLAGS) and s["policy"] == "deterministic_only" and s["steps"] == []
    pair(engine, world)
    s = engine.ai_settings_save(world.A, world.owner, {"policy": "local_only"})
    assert s["policy"] == "local_only" and [x["step"] for x in s["steps"]] == ["local"]  # AI on means local, alone
    assert engine.ai_settings_save(world.A, world.owner, {"policy": "deterministic_only"})["steps"] == []
    s = engine.ai_settings_save(world.A, world.owner, {"policy": "local_only", "fallback_free": False})  # (named flags refine a preset; with nothing else on, it is still the preset)
    assert s["policy"] == "local_only"


def test_a_step_that_sends_evidence_outside_cannot_be_turned_on_without_the_businesss_agreement(engine, db, world):
    pair(engine, world)
    for policy in ("local_free", "local_free_paid"):
        with pytest.raises(svc.ServiceError) as e:
            engine.ai_settings_save(world.A, world.owner, {"policy": policy, "paid_cap_usd": 5})
        assert e.value.status == 409 and "agreed" in e.value.message  # the business has not agreed to the provider's text
    assert engine.store.ai_settings(world.A)["fallback_free"] is False
    agree(engine, world, "openrouter")
    assert engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})["policy"] == "local_free"
    with pytest.raises(svc.ServiceError) as e:  # the paid step is a different provider, so a different agreement, and it also needs the business's own spending cap
        engine.ai_settings_save(world.A, world.owner, {"policy": "local_free_paid", "paid_cap_usd": 5})
    assert e.value.status == 409
    agree(engine, world, "cloudflare")
    with pytest.raises(svc.ServiceError) as e:
        engine.ai_settings_save(world.A, world.owner, {"policy": "local_free_paid"})
    assert e.value.status == 400 and "spending limit" in e.value.message
    s = engine.ai_settings_save(world.A, world.owner, {"policy": "local_free_paid", "paid_cap_usd": 5})
    assert s["policy"] == "local_free_paid" and [x["step"] for x in s["steps"]] == ["local", "free", "paid"] and s["settings"]["paid_cap_usd"] == 5


def test_only_an_owner_or_admin_can_change_the_policy_and_bad_values_are_refused(engine, world):
    pair(engine, world)
    for who in (world.editor, world.viewer):
        with pytest.raises(svc.ServiceError) as e:
            engine.ai_settings_save(world.A, who, {"policy": "local_only"})
        assert e.value.status == 403
    for body in ({"policy": "cloud_first"}, {"policy": ["local_only"]}, {"mode": "local", "fallback_free": "yes"}, {"mode": "local", "paid_cap_usd": -1}, {"mode": "local", "paid_cap_usd": True},
                 {"mode": "local", "paid_cap_usd": 10**9}):
        with pytest.raises(svc.ServiceError) as e:
            engine.ai_settings_save(world.A, world.owner, body)
        assert e.value.status == 400, body


def test_the_operators_switch_still_stops_a_step_the_business_turned_on(engine, db, world, free_server):
    pair(engine, world)
    agree(engine, world, "openrouter")
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    plan_row(db, world)
    db.su("update public.local_companions set revoked_at = now() where business_id = %s", (world.A,))  # the computer is gone: the free step is next
    engine.ai_modes = frozenset({"local", "byok"})  # the operator switches platform inference off
    engine.router.enabled_modes = engine.ai_modes
    ex = ask(engine, world)["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and calls(free_server) == []
    assert [a["reason"] for a in ex["attempts"]] == ["local:no_companion", "config:ai_disabled"]


# ================================================================== local first, then the steps the business enabled
def test_local_is_the_first_step_and_nothing_is_sent_while_it_is_usable(engine, db, world, free_server, paid_server):
    pair(engine, world)
    agree(engine, world, "openrouter")
    agree(engine, world, "cloudflare")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free_paid", "paid_cap_usd": 5})
    ex = ask(engine, world)["execution"]  # a plain request: this client has not run the local model, so the deterministic answer is given and the browser is told local is available
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "local:client_inference_required" and ex["local"]["available"] is True
    assert calls(free_server) == [] and calls(paid_server) == []  # the hosted steps are not asked just because the local step needs the browser
    out = ask_local(engine, world)
    assert out["execution"]["generated_by"] == "model" and out["execution"]["mode"] == "local" and out["execution"]["tier"] == "local" and out["verified"] is True
    assert calls(free_server) == [] and calls(paid_server) == []


def test_when_local_fails_the_free_step_answers_and_the_answer_says_so(engine, db, world, free_server, paid_server):
    pair(engine, world)
    agree(engine, world, "openrouter")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    out = ask_local(engine, world, failure="timeout")
    ex = out["execution"]
    assert ex["generated_by"] == "model" and ex["mode"] == "platform" and ex["tier"] == "free" and ex["provider"] == "openrouter" and out["verified"] is True
    assert ex["fallback_from"] == {"step": "local", "provider": "ollama", "reason": "provider:timeout"}
    assert [(a["step"], a["outcome"]) for a in ex["attempts"]] == [("local", "unavailable"), ("free", "answered")]
    assert len(calls(free_server)) == 1 and calls(free_server)[0]["headers"]["Authorization"] == f"Bearer {FREE_KEY}" and calls(paid_server) == []


def test_stopping_a_local_answer_is_not_a_failure_and_sends_nothing_anywhere_even_with_hosted_steps_turned_on(engine, db, world, free_server, paid_server):
    both_hosted(engine, db, world)
    db.su("update public.local_companions set revoked_at = null where business_id = %s", (world.A,))  # the computer is there again
    ex = ask_local(engine, world, failure="cancelled")["execution"]
    assert ex["failure_kind"] == "provider:cancelled" and ex["generated_by"] == "deterministic_fallback" and ex["tier"] == "local" and [a["outcome"] for a in ex["attempts"]] == ["cancelled"]
    assert calls(free_server) == [] and calls(paid_server) == []  # the person pressed Cancel: the question did not go anywhere


def test_a_failed_local_step_sends_nothing_when_the_business_did_not_turn_a_follower_on(engine, world, free_server, paid_server, byok_server):
    pair(engine, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_only"})
    ex = ask_local(engine, world, failure="not_connected")["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "provider:not_connected" and ex["tier"] == "local" and "fallback_from" not in ex
    assert calls(free_server) == [] and calls(paid_server) == [] and calls(byok_server) == []


def test_a_follower_whose_agreement_was_withdrawn_is_skipped_and_nothing_is_sent_to_it(engine, db, world, free_server, paid_server):
    pair(engine, world)
    agree(engine, world, "openrouter")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    withdraw(db, world, "openrouter")  # the business withdrew its agreement after turning the step on
    out = ask_local(engine, world, failure="timeout")
    ex = out["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "provider:timeout"  # the answer stays what the computer's failure made it
    assert [(a["step"], a["outcome"], a["reason"]) for a in ex["attempts"]] == [("local", "unavailable", "provider:timeout"), ("free", "refused", "consent:missing_or_revoked")]
    assert calls(free_server) == [] and calls(paid_server) == []


def test_a_computer_that_said_it_cannot_run_the_model_is_not_asked_and_the_next_step_answers(engine, db, world, free_server):
    pair(engine, world, fit="comfortable")
    agree(engine, world, "openrouter")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    db.su("update public.local_companions set revoked_at = now() where business_id = %s", (world.A,))
    pair(engine, world, fit="not_advised")
    ex = ask(engine, world)["execution"]
    assert [(a["step"], a["reason"]) for a in ex["attempts"][:1]] == [("local", "local:unsuitable_hardware")]
    assert ex["generated_by"] == "model" and ex["tier"] == "free" and len(calls(free_server)) == 1


def test_with_no_companion_at_all_the_enabled_hosted_step_answers_directly(engine, db, world, free_server):
    pair(engine, world)
    agree(engine, world, "openrouter")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    db.su("update public.local_companions set revoked_at = now() where business_id = %s", (world.A,))
    ex = ask(engine, world)["execution"]
    assert ex["generated_by"] == "model" and ex["tier"] == "free" and ex["fallback_from"]["reason"] == "local:no_companion"


# ================================================================== free before paid, quotas and the spending cap
def both_hosted(engine, db, world, **plan):
    pair(engine, world)
    agree(engine, world, "openrouter")
    agree(engine, world, "cloudflare")
    plan_row(db, world, **plan)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free_paid", "paid_cap_usd": 5})
    db.su("update public.local_companions set revoked_at = now() where business_id = %s", (world.A,))  # so that the hosted steps are the ones in play


def test_free_is_tried_before_paid_and_paid_is_not_asked_when_free_answers(engine, db, world, free_server, paid_server):
    both_hosted(engine, db, world)
    ex = ask(engine, world)["execution"]
    assert ex["tier"] == "free" and len(calls(free_server)) == 1 and calls(paid_server) == []
    row = db.su("select tier, mode, provider, status from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,))
    assert row == [("free", "platform", "openrouter", "ok")]


def test_when_the_free_provider_fails_the_paid_step_answers_and_both_calls_are_metered_on_their_own_steps(engine, db, world, free_server, paid_server):
    both_hosted(engine, db, world)
    free_server.status = 500
    ex = ask(engine, world)["execution"]
    assert ex["generated_by"] == "model" and ex["tier"] == "paid" and ex["provider"] == "cloudflare" and ex["fallback_from"]["step"] == "local"
    assert [(a["step"], a["outcome"]) for a in ex["attempts"]] == [("local", "refused"), ("free", "unavailable"), ("paid", "answered")]
    rows = sorted(db.su("select tier, status, provider from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,)))
    assert rows == [("free", "failed", "openrouter"), ("paid", "ok", "cloudflare")]
    assert len(calls(paid_server)) == 1 and calls(paid_server)[0]["headers"]["Authorization"] == f"Bearer {PAID_KEY}"


def test_an_exhausted_free_quota_moves_to_paid_without_calling_the_free_provider(engine, db, world, free_server, paid_server):
    both_hosted(engine, db, world, free=(1, 100_000))
    assert ask(engine, world)["execution"]["tier"] == "free"
    free_server.requests.clear()
    ex = ask(engine, world)["execution"]
    assert ex["tier"] == "paid" and calls(free_server) == []
    assert [(a["step"], a["reason"]) for a in ex["attempts"] if a["outcome"] == "refused"] == [("local", "local:no_companion"), ("free", "limit:free_quota_requests")]


def test_a_plan_with_no_free_allowance_has_no_free_hosted_inference(engine, db, world, free_server):
    both_hosted(engine, db, world)
    db.su("update public.business_plans set free_requests_per_month = null where business_id = %s", (world.A,))
    ex = ask(engine, world)["execution"]
    assert ex["tier"] == "paid" and calls(free_server) == []
    assert ("free", "limit:no_free_quota") in [(a["step"], a["reason"]) for a in ex["attempts"]]


def test_the_paid_step_stops_at_the_businesss_own_spending_cap_before_the_plans(engine, db, world, free_server, paid_server):
    both_hosted(engine, db, world, free=(None, None))
    db.su("update public.business_plans set free_requests_per_month = null, free_tokens_per_month = null where business_id = %s", (world.A,))
    db.su("update public.ai_inference_settings set paid_cap_usd = 0.04 where business_id = %s", (world.A,))  # one call costs 0.025 at the test prices: two calls pass 0.04
    assert ask(engine, world)["execution"]["tier"] == "paid"
    assert ask(engine, world)["execution"]["tier"] == "paid"
    paid_server.requests.clear()
    ex = ask(engine, world)["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and calls(paid_server) == []
    assert ("paid", "limit:spend_cap") in [(a["step"], a["reason"]) for a in ex["attempts"]]
    summary = engine.usage_get(world.A, world.owner)["summary"]
    assert summary["spend_cap_usd"] == pytest.approx(0.04) and summary["spend_cap_remaining_usd"] == 0.0 and summary["used_paid"]["requests"] == 2 and summary["used_free"]["requests"] == 0


def test_free_calls_never_count_against_the_paid_limits(engine, db, world, free_server):
    both_hosted(engine, db, world, paid=(1, 100_000, 50))
    for _ in range(3):
        assert ask(engine, world)["execution"]["tier"] == "free"
    assert engine.limits.check(world.A, "platform", "paid") is None  # the paid allowance of one request is untouched


# ================================================================== a bad answer is not a reason to send the evidence elsewhere
def test_a_model_that_answers_badly_does_not_hand_the_evidence_to_the_next_provider(engine, db, world, byok_server, free_server):
    set_up_byok(engine, db, world, byok_server)
    agree(engine, world, "openai_compatible")
    agree(engine, world, "openrouter")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "byok_preferred", "fallback_free": True})
    byok_server.reply = lambda body: "I am sure it is fine."  # not JSON: the model answered, badly
    ex = ask(engine, world)["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "reasoning:invalid_json"
    assert len(calls(byok_server)) == 1 and calls(free_server) == []
    byok_server.reply = claims_reply()
    byok_server.status = 500  # the provider itself failing is another matter: the step the business enabled next gets its turn
    ex = ask(engine, world)["execution"]
    assert ex["tier"] == "free" and ex["fallback_from"]["step"] == "byok"


def test_byok_as_an_alternative_comes_after_local_and_before_the_hosted_steps(engine, db, world, byok_server, free_server):
    pair(engine, world)
    set_up_byok(engine, db, world, byok_server)
    agree(engine, world, "openai_compatible")
    agree(engine, world, "openrouter")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "byok_alternative", "fallback_free": True})
    assert [x["step"] for x in engine.ai_settings_get(world.A, world.owner)["steps"]] == ["local", "byok", "free"]
    ex = ask_local(engine, world, failure="timeout")["execution"]
    assert ex["tier"] == "byok" and ex["provider"] == "openai_compatible" and len(calls(byok_server)) == 1 and calls(free_server) == []
    assert calls(byok_server)[0]["headers"]["Authorization"] == f"Bearer {BYOK_KEY}"


def test_byok_as_the_preferred_provider_is_first_and_local_is_not_used(engine, db, world, byok_server, free_server):
    pair(engine, world)
    set_up_byok(engine, db, world, byok_server)
    agree(engine, world, "openai_compatible")
    engine.ai_settings_save(world.A, world.owner, {"policy": "byok_preferred"})
    assert [x["step"] for x in engine.ai_settings_get(world.A, world.owner)["steps"]] == ["byok"]
    ex = ask(engine, world)["execution"]
    assert ex["tier"] == "byok" and ex["generated_by"] == "model" and len(calls(byok_server)) == 1


# ================================================================== the record
def test_the_audit_trail_records_who_turned_a_step_on_and_the_spending_cap(engine, db, world):
    pair(engine, world)
    agree(engine, world, "openrouter")
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    detail = db.su("select detail from public.audit_logs where business_id = %s and target_type = 'ai_inference_settings' order by created_at desc limit 1", (world.A,))[0][0]
    assert detail["free_step_after"] == "true" and detail["free_step_before"] == "false" and detail["paid_step_after"] == "false" and detail["mode_after"] == "local"


def test_history_shows_which_step_answered_and_why_the_earlier_ones_did_not(engine, db, world):
    pair(engine, world)
    agree(engine, world, "openrouter")
    plan_row(db, world)
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    db.su("update public.local_companions set revoked_at = now() where business_id = %s", (world.A,))
    ask(engine, world)
    item = engine.history_get(world.A, world.viewer, 5)["items"][0]["execution"]
    assert item["tier"] == "free" and item["provider"] == "openrouter" and item["fallback_from"]["reason"] == "local:no_companion"
    assert [a["step"] for a in item["attempts"]] == ["local", "free"] and all("usage" in a for a in item["attempts"])


def test_another_business_is_not_affected_by_this_ones_policy(engine, db, world, free_server):
    other_owner = str(db.user("lf-other"))
    other = str(db.business(other_owner, "localfirst-b"))
    assert engine.ai_settings_get(other, other_owner)["policy"] == "deterministic_only"
    pair(engine, world)
    agree(engine, world, "openrouter")
    engine.ai_settings_save(world.A, world.owner, {"policy": "local_free"})
    assert engine.ai_settings_get(other, other_owner)["settings"]["fallback_free"] is False
    with pytest.raises(svc.ServiceError) as e:
        engine.ai_settings_save(other, other_owner, {"policy": "local_free"})  # this business's agreement is not that business's
    assert e.value.status == 409
