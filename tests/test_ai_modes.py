"""The three execution modes in the hosted service, on the real schema with the real embedding model and stand-in providers over real HTTP: platform, the business's own provider (BYOK) and the
business's own machine (local), all through the SAME pipeline. What is proved: the router's decisions (the operator's switch, the business's choice, verification, consent, plan limits, shared
capacity, the circuit breaker), that nothing fails over to another provider or mode, that usage is metered per mode, that local inference never reaches a provider, and that none of it crosses
from one business to another. No real provider is called."""
import json
import re
import sys
import time
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("cryptography")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_provider import FakeProvider  # noqa: E402
from pgdb import make_db  # noqa: E402

from faqengine.hosted import service as svc  # noqa: E402
from faqengine.hosted import telemetry  # noqa: E402
from faqengine.hosted.secrets import SecretBox  # noqa: E402
from faqengine.hosted.store import Store  # noqa: E402
from faqengine.inference import registry  # noqa: E402
from faqengine.inference.resilience import Breakers  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
pytestmark = pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
FAQ_CSV = (ROOT / "examples" / "bike-shop" / "faq.csv").read_bytes()
PLATFORM_KEY = "sk-platform-key-909090"
BYOK_KEY = "sk-customer-key-717171"
HOURS = "What are your opening hours?"


def claims_reply(tamper=None):
    """A model that answers in the pipeline's JSON: it restates the first numbered evidence item, or says what `tamper` says about it."""
    def reply(body):
        text = " ".join(m.get("content", "") for m in body.get("messages", []))
        m = re.search(r"\[(E\d+)\] \([^)]*\) (.*)", text)
        if not m:
            return json.dumps({"verdict": "insufficient", "claims": [], "missing": ["everything"]})
        return json.dumps({"verdict": "answered", "claims": [{"text": tamper or re.split(r"(?<=[.!?])\s", m.group(2).strip())[0], "evidence": [m.group(1)]}], "missing": []})
    return reply


class Files(dict):
    def __call__(self, path):
        return self[path]


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def platform_server():
    p = FakeProvider(key=PLATFORM_KEY, reply=claims_reply())
    yield p
    p.close()


@pytest.fixture(scope="module")
def other_server():
    """A provider nobody chose: whatever happens, it must never be called."""
    p = FakeProvider(key="sk-nobody-chose-this-000", reply=claims_reply())
    yield p
    p.close()


@pytest.fixture(scope="module")
def catalogue_file(tmp_path_factory):
    f = tmp_path_factory.mktemp("cat") / "catalogue.json"
    f.write_text(json.dumps({"models": [
        {"provider": "openrouter", "id": "chat-small", "label": "Test model", "structured": "unverified", "verified_on": "2026-11-02", "source": "https://example.invalid/models",
         "platform_offered": True, "tier": "paid", "price_in_per_m": 1000.0, "price_out_per_m": 2000.0},
        {"provider": "cloudflare", "id": "not-offered", "label": "Candidate", "structured": "unverified", "verified_on": "2026-11-02", "source": "https://example.invalid/models", "platform_offered": False}]}))
    return f


@pytest.fixture(autouse=True)
def _env(verified_providers, catalogue_file, platform_server, monkeypatch):
    monkeypatch.setenv("MODEL_CATALOGUE_FILE", str(catalogue_file))
    monkeypatch.setitem(registry.PROVIDERS["openrouter"], "base_url", platform_server.url())  # the stand-in plays OpenRouter


@pytest.fixture(scope="module")
def engine(db):
    box = SecretBox({1: SecretBox.generate() and __import__("base64").b64decode(SecretBox.generate())})
    platform = registry.PlatformCredentials((("PLATFORM_OPENROUTER_API_KEY", PLATFORM_KEY),))
    return svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), allow_private_llm=True, rate_per_minute=10_000, ai_modes={"platform", "byok", "local"}, platform=platform,
                            breakers=Breakers(failures=3, cooldown=60), inference_per_minute=100, pending_ttl=60.0, fast_path=False)  # these tests are about the MODEL path (consent, routing, limits, metering, verification): the deterministic fast path has its own tests (test_fast_path.py)


@pytest.fixture(scope="module")
def world(db, engine):
    w = type("W", (), {})()
    w.owner, w.admin, w.editor, w.viewer, w.other, w.other_viewer = (str(db.user(n)) for n in ("a-owner", "a-admin", "a-editor", "a-viewer", "a-other", "a-other-viewer"))
    w.A, w.B = str(db.business(w.owner, "modes-a")), str(db.business(w.other, "modes-b"))
    db.member(w.A, w.admin, "admin")
    db.member(w.A, w.editor, "editor")
    db.member(w.A, w.viewer, "viewer")
    db.member(w.B, w.other_viewer, "viewer")
    for biz, who in ((w.A, w.owner), (w.B, w.other)):
        engine.fetch_object[f"{biz}/faq.csv"] = FAQ_CSV
        sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, 'faq.csv', 'faq_table', %s) returning id", (biz, f"{biz}/faq.csv"))[0][0])
        engine.ingest(biz, who, sid)
    return w


@pytest.fixture(autouse=True)
def clean(db, engine, world, platform_server, other_server):
    """Every test starts from: deterministic mode, no consent, no plan, no usage, no companions, closed circuits, no scripted failures."""
    yield
    for b, u in ((world.A, world.owner), (world.B, world.other)):
        engine.store.save_ai_settings(b, u, "deterministic", None, None, "deterministic")
        db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where business_id = %s and status = 'granted'", (b,))
        db.su("delete from public.business_plans where business_id = %s", (b,))
        db.su("delete from public.usage_records where business_id = %s and kind = 'llm_call'", (b,))
        db.su("delete from public.llm_provider_secrets where business_id = %s", (b,))
        db.su("delete from public.llm_provider_configs where business_id = %s", (b,))
        db.su("update public.local_companions set revoked_at = now() where business_id = %s and revoked_at is null", (b,))
    engine.breakers.__init__(failures=3, cooldown=60)
    engine.pipeline_mode = "off"
    engine.provider_factory = None
    engine.ai_modes = frozenset({"platform", "byok", "local"})
    engine.router.enabled_modes = engine.ai_modes
    engine._pending.clear()
    engine.pending_ttl = 60.0
    for p in (platform_server, other_server):
        p.status, p.statuses, p.reply, p.delay, p.reject = None, [], claims_reply(), 0.0, set()
        p.requests.clear()


def completions(server):
    return [r for r in server.requests if r["method"] == "POST" and r["path"].endswith("/chat/completions")]


def agree(engine, biz, owner, provider):
    engine.consent_grant(biz, owner, provider, svc.disclosure.current_version(provider))


def platform_ready(engine, db, world, **plan):
    agree(engine, world.A, world.owner, "openrouter")
    db.su("insert into public.business_plans (business_id, plan, inference_requests_per_month, tokens_per_month, cost_cap_usd_per_month, inference_per_minute) values (%s, %s, %s, %s, %s, %s)",
          (world.A, plan.get("plan", "paid"), plan.get("requests", 1000), plan.get("tokens", 1_000_000), plan.get("cost", 100), plan.get("per_minute", 100)))
    return engine.ai_settings_save(world.A, world.owner, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "chat-small"})


def ask(engine, biz, user, q=HOURS):
    return engine.respond(biz, user, q)["result"]


# ================================================================== the router: decisions, in order
def test_the_default_is_deterministic_and_sends_nothing_anywhere(engine, world, platform_server):
    r = ask(engine, world.A, world.viewer)
    ex = r["execution"]
    assert ex["mode"] == "deterministic" and ex["generated_by"] == "deterministic" and ex["inference_attempted"] is False and completions(platform_server) == []
    assert engine.router.route(world.A).mode == "deterministic"


def test_the_operators_switch_beats_everything_a_business_has_chosen_or_agreed(engine, db, world, platform_server):
    platform_ready(engine, db, world)
    engine.ai_modes = frozenset()
    engine.router.enabled_modes = frozenset()
    r = engine.router.route(world.A)
    assert r.mode == "platform" and r.refusal == "config:ai_disabled"
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "config:ai_disabled" and completions(platform_server) == []
    with pytest.raises(svc.ServiceError) as e:
        engine.ai_settings_save(world.A, world.owner, {"mode": "byok"})
    assert e.value.status == 409 and "not switched on" in e.value.message


def test_only_a_model_released_on_purpose_can_be_chosen_for_platform_mode(engine, world):
    for body in ({"mode": "platform"}, {"mode": "platform", "platform_provider": "cloudflare", "platform_model": "not-offered"}, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "invented/model"},
                 {"mode": "platform", "platform_provider": 5, "platform_model": ["x"]}):
        with pytest.raises(svc.ServiceError) as e:
            engine.ai_settings_save(world.A, world.owner, body)
        assert e.value.status == 409 and "not offered" in e.value.message, body
    assert engine.store.ai_settings(world.A)["mode"] == "deterministic"


def test_platform_mode_needs_the_platforms_own_credential(engine, db, world):
    agree(engine, world.A, world.owner, "openrouter")
    engine.ai_settings_save(world.A, world.owner, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "chat-small"})
    saved = engine.router.platform
    engine.router.platform = registry.PlatformCredentials()
    try:
        assert engine.router.route(world.A).refusal == "config:platform_unavailable"
    finally:
        engine.router.platform = saved


def test_consent_and_verified_terms_come_before_limits_and_before_any_credential_is_opened(engine, db, world, platform_server, monkeypatch):
    engine.ai_settings_save(world.A, world.owner, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "chat-small"})
    r = engine.router.route(world.A)
    assert r.refusal == "consent:missing_or_revoked"  # no plan exists either: the consent is what is said first
    agree(engine, world.A, world.owner, "openrouter")
    assert engine.router.route(world.A).refusal == "limit:no_plan"  # now the plan
    monkeypatch.delenv("AI_DISCLOSURE_FILE")  # the entries become unverified again
    assert engine.router.route(world.A).refusal == "consent:provider_unverified"
    assert completions(platform_server) == []


def test_a_plan_with_a_limit_left_empty_is_not_unlimited(engine, db, world):
    agree(engine, world.A, world.owner, "openrouter")
    db.su("insert into public.business_plans (business_id, plan, inference_requests_per_month) values (%s, 'paid', 10)", (world.A,))  # tokens and cost cap stated nowhere
    engine.ai_settings_save(world.A, world.owner, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "chat-small"})
    assert engine.router.route(world.A).refusal == "limit:no_plan"


# ================================================================== platform mode, end to end
def test_platform_mode_uses_the_platforms_key_and_meters_the_use(engine, db, world, platform_server):
    platform_ready(engine, db, world)
    r = engine.respond(world.A, world.viewer, HOURS)
    res, ex = r["result"], r["result"]["execution"]
    assert ex["generated_by"] == "model" and ex["mode"] == "platform" and ex["provider"] == "openrouter" and ex["model"] == "chat-small" and res["verified"] is True and res["answer"].startswith("We are open")
    sent = completions(platform_server)[-1]
    assert sent["headers"]["Authorization"] == f"Bearer {PLATFORM_KEY}" and BYOK_KEY not in json.dumps(sent)
    row = db.su("select mode, provider, model, tokens_in, tokens_out, cost, status, correlation_id, generated_by from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,))[0]
    assert row[:3] == ("platform", "openrouter", "chat-small") and row[3] == 11 and row[4] == 7 and float(row[5]) == pytest.approx(11 / 1e6 * 1000 + 7 / 1e6 * 2000) and row[6] == "ok" and row[7] == ex["correlation_id"]
    summary = engine.usage_get(world.A, world.owner)
    assert summary["summary"]["used"]["platform"]["requests"] == 1 and summary["summary"]["remaining_platform"]["requests"] == 999 and summary["summary"]["platform_allowed"] is True


def test_the_monthly_request_limit_stops_the_next_call_and_says_so(engine, db, world, platform_server):
    platform_ready(engine, db, world, requests=2)
    for _ in range(2):
        assert ask(engine, world.A, world.viewer)["execution"]["generated_by"] == "model"
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "limit:quota_requests" and len(completions(platform_server)) == 2  # the third never reached the provider
    assert engine.usage_get(world.A, world.owner)["summary"]["remaining_platform"]["requests"] == 0


@pytest.mark.parametrize("plan,why", [({"tokens": 30}, "limit:quota_tokens"), ({"cost": 0.0001}, "limit:quota_cost")])
def test_token_and_cost_limits_are_enforced_from_recorded_usage(engine, db, world, plan, why):
    platform_ready(engine, db, world, **plan)
    ask(engine, world.A, world.viewer)  # 18 tokens, about 0.000025 USD
    ask(engine, world.A, world.viewer)
    ask(engine, world.A, world.viewer)
    assert ask(engine, world.A, world.viewer)["execution"]["failure_kind"] == why


def test_a_trial_that_has_ended_allows_no_platform_inference(engine, db, world):
    platform_ready(engine, db, world, plan="trial")
    db.su("update public.business_plans set trial_ends_at = now() - interval '1 day' where business_id = %s", (world.A,))
    assert ask(engine, world.A, world.viewer)["execution"]["failure_kind"] == "limit:trial_ended"


def test_a_business_cannot_exceed_its_per_minute_allowance(engine, db, world, platform_server):
    platform_ready(engine, db, world, per_minute=2)
    for _ in range(2):
        assert ask(engine, world.A, world.viewer)["execution"]["generated_by"] == "model"
    assert ask(engine, world.A, world.viewer)["execution"]["failure_kind"] == "limit:rate_limited" and len(completions(platform_server)) == 2


def test_the_platforms_shared_capacity_is_protected_by_a_cap_on_calls_in_flight(engine, db, world, platform_server):
    platform_ready(engine, db, world)
    held = [engine.limits.take_platform_slot() for _ in range(8)]
    try:
        assert all(held)
        ex = ask(engine, world.A, world.viewer)["execution"]
        assert ex["failure_kind"] == "limit:platform_busy" and ex["generated_by"] == "deterministic_fallback" and completions(platform_server) == []
    finally:
        for _ in held:
            engine.limits.release_platform_slot()
    assert ask(engine, world.A, world.viewer)["execution"]["generated_by"] == "model"


def test_one_business_exhausting_its_plan_does_not_touch_another_business(engine, db, world, platform_server):
    platform_ready(engine, db, world, requests=1)
    ask(engine, world.A, world.viewer)
    assert ask(engine, world.A, world.viewer)["execution"]["failure_kind"] == "limit:quota_requests"
    agree(engine, world.B, world.other, "openrouter")
    db.su("insert into public.business_plans (business_id, plan, inference_requests_per_month, tokens_per_month, cost_cap_usd_per_month) values (%s, 'paid', 5, 100000, 50)", (world.B,))
    engine.ai_settings_save(world.B, world.other, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "chat-small"})
    assert ask(engine, world.B, world.other_viewer, "Do you groom cats?")["execution"]["mode"] == "platform"
    assert engine.usage_get(world.B, world.other)["summary"]["used"]["platform"]["requests"] <= 1  # B's own count


# ================================================================== failure: no failover, ever
def test_a_failing_provider_opens_the_circuit_and_nothing_else_is_tried(engine, db, world, platform_server, other_server):
    platform_ready(engine, db, world)
    platform_server.status = 503
    first = ask(engine, world.A, world.viewer)["execution"]
    assert first["generated_by"] == "deterministic_fallback" and first["failure_kind"] == "provider:server" and first["usage"]["attempts"] == 2  # a call and one retry of the same provider
    second = ask(engine, world.A, world.viewer)["execution"]  # its first call is the third failure in a row: the circuit opens, and its retry meets the open circuit
    assert second["failure_kind"] == "provider:circuit_open" and second["generated_by"] == "deterministic_fallback"
    seen = len(completions(platform_server))
    assert seen == 3 and engine.breakers.states() == {"platform/openrouter/chat-small": "open"}
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["failure_kind"] == "provider:circuit_open" and ex["generated_by"] == "deterministic_fallback" and len(completions(platform_server)) == seen  # not asked again
    assert completions(other_server) == []  # and no other provider was tried in its place
    assert engine.metrics()["circuits"] == {"platform/openrouter/chat-small": "open"}


def test_with_fallback_none_a_failed_ai_answer_says_it_is_unavailable_instead_of_quietly_being_deterministic(engine, db, world, platform_server):
    platform_ready(engine, db, world)
    engine.ai_settings_save(world.A, world.owner, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "chat-small", "fallback": "none"})
    platform_server.status = 500
    r = ask(engine, world.A, world.viewer)
    ex = r["execution"]
    assert ex["generated_by"] == "unavailable" and r["outcome"] == "insufficient" and "unavailable" in r["message"] and r["answer"] is None and ex["validation"] == "not_verified"
    db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where business_id = %s and status = 'granted'", (world.A,))
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["generated_by"] == "deterministic_no_consent"  # without consent nothing was tried, so there is no failure to report: the deterministic answer, said as such


def test_a_byok_business_is_never_moved_to_the_platform_and_a_platform_business_never_to_its_own_key(engine, db, world, platform_server, other_server):
    # a business that chose its own provider, refused (no consent), with the platform fully set up and offered
    db.su("insert into public.llm_provider_configs (business_id, provider, model, base_url, enabled, has_key) values (%s, 'openai_compatible', 'chat-small', %s, true, true)", (world.A, other_server.url()))
    row = engine.secrets.encrypt(BYOK_KEY, world.A)
    db.su("insert into public.llm_provider_secrets (business_id, encrypted_api_key, key_version) values (%s, %s, %s)", (world.A, row[0], row[1]))
    agree(engine, world.A, world.owner, "openrouter")
    db.su("insert into public.business_plans (business_id, plan, inference_requests_per_month, tokens_per_month, cost_cap_usd_per_month) values (%s, 'paid', 100, 100000, 50)", (world.A,))
    engine.ai_settings_save(world.A, world.owner, {"mode": "byok"})
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["mode"] == "byok" and ex["failure_kind"] == "consent:missing_or_revoked" and completions(platform_server) == [] and completions(other_server) == []
    # and the other way round: platform chosen, the saved own key stays untouched
    engine.ai_settings_save(world.A, world.owner, {"mode": "platform", "platform_provider": "openrouter", "platform_model": "chat-small"})
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["mode"] == "platform" and ex["generated_by"] == "model" and completions(other_server) == []


def test_a_model_that_invents_evidence_or_facts_never_gets_through_in_any_mode(engine, db, world, platform_server):
    platform_ready(engine, db, world)
    platform_server.reply = lambda body: json.dumps({"verdict": "answered", "claims": [{"text": "We are open Monday to Saturday.", "evidence": ["E99"]}], "missing": []})  # an id nobody issued
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "reasoning:unsupported_claims" and ex["inference_succeeded"] is False
    platform_server.reply = lambda body: "I am sure it is fine."  # not JSON
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["failure_kind"] == "reasoning:invalid_json" and ex["generated_by"] == "deterministic_fallback"


# ================================================================== settings, usage, history: who may do what
def test_who_may_read_and_change_how_a_business_answers(engine, world):
    for who in (world.editor, world.viewer):
        with pytest.raises(svc.ServiceError) as e:
            engine.ai_settings_save(world.A, who, {"mode": "deterministic"})
        assert e.value.status == 403
        with pytest.raises(svc.ServiceError) as e:
            engine.usage_get(world.A, who)
        assert e.value.status == 403  # usage is for owners and admins
    assert engine.ai_settings_get(world.A, world.viewer)["settings"]["mode"] == "deterministic"  # but anyone in the business can see where it stands
    for call in (lambda: engine.ai_settings_get(world.A, world.other), lambda: engine.usage_get(world.A, world.other), lambda: engine.history_get(world.A, world.other),
                 lambda: engine.ai_settings_save(world.A, world.other, {"mode": "deterministic"}), lambda: engine.companions_get(world.A, world.other)):
        with pytest.raises(svc.ServiceError) as e:
            call()
        assert e.value.status == 403  # another business's owner
    with pytest.raises(svc.ServiceError) as e:
        engine.ai_settings_save(world.A, world.owner, {"mode": "deterministic", "fallback": "another-provider"})
    assert e.value.status == 400  # there is no failover to choose


def test_every_change_of_mode_is_in_the_audit_trail(db, engine, world):
    engine.ai_settings_save(world.A, world.admin, {"mode": "deterministic", "fallback": "none"})
    rows = db.su("select actor_id::text, detail from public.audit_logs where business_id = %s and target_type = 'ai_inference_settings' order by created_at desc limit 1", (world.A,))[0]
    assert rows[0] == world.admin and rows[1]["mode_after"] == "deterministic" and rows[1]["fallback_after"] == "none"


def test_history_shows_who_generated_each_answer_and_respects_the_privacy_setting(db, engine, world):
    ask(engine, world.A, world.viewer, "How much does a puncture repair cost?")
    items = engine.history_get(world.A, world.viewer, 5)["items"]
    assert items and items[0]["execution"]["generated_by"] == "deterministic" and items[0]["execution"]["mode"] == "deterministic" and "puncture" in items[0]["question"]
    assert engine.history_get(world.B, world.other)["items"] == [] or all("puncture" not in (i["question"] or "") for i in engine.history_get(world.B, world.other)["items"])
    with pytest.raises(svc.ServiceError):
        engine.history_get(world.A, world.viewer, 5, "not a time")
    assert len(engine.history_get(world.A, world.viewer, 100000)["items"]) <= 200


# ================================================================== local mode: the evidence goes to the person's own machine, to no provider
def pair(engine, world, **kw):
    return engine.companion_register(world.A, world.owner, {"label": "my laptop", "platform": "macOS 15 arm64", "runtime": "ollama", "model": "qwen3:4b", **kw})


def test_local_mode_needs_a_paired_companion_and_only_an_admin_can_pair_one(engine, world):
    with pytest.raises(svc.ServiceError) as e:
        engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    assert e.value.status == 409 and "pair a companion" in e.value.message
    for who in (world.editor, world.viewer, world.other):
        with pytest.raises(svc.ServiceError) as e:
            engine.companion_register(world.A, who, {"label": "x", "runtime": "ollama", "model": "m"})
        assert e.value.status == 403
    for bad in ({"label": ""}, {"label": "x" * 81}, {"runtime": "lmstudio"}, {"model": ""}, {"platform": "p" * 81}, {"label": "tab\there"}):
        with pytest.raises(svc.ServiceError) as e:
            pair(engine, world, **bad)
        assert e.value.status == 400, bad
    made = pair(engine, world)
    assert len(made["companions"]) == 1 and "secret" not in json.dumps(made).lower()
    assert engine.ai_settings_save(world.A, world.owner, {"mode": "local"})["route"] == {"mode": "local", "usable": True, "refusal": None, "provider": "ollama", "model": "qwen3:4b"}
    for _ in range(4):
        pair(engine, world)
    with pytest.raises(svc.ServiceError) as e:
        pair(engine, world)
    assert e.value.status == 409  # five at most


def test_revoking_the_last_companion_ends_local_mode_instead_of_leaving_it_half_configured(engine, world):
    cid = pair(engine, world)["id"]
    engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    for who in (world.editor, world.other):
        with pytest.raises(svc.ServiceError) as e:
            engine.companion_revoke(world.A, who, cid)
        assert e.value.status == 403
    after = engine.companion_revoke(world.A, world.owner, cid)
    assert after["companions"][0]["revoked"] is True and engine.store.ai_settings(world.A)["mode"] == "deterministic"


def test_a_plain_respond_in_local_mode_gives_the_deterministic_answer_and_says_local_inference_did_not_run(engine, world, platform_server, other_server):
    pair(engine, world)
    engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    ex = ask(engine, world.A, world.viewer)["execution"]
    assert ex["mode"] == "local" and ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == "local:client_inference_required" and ex["local"]["available"] is True
    assert completions(platform_server) == [] and completions(other_server) == []  # a local business is never answered by a hosted model instead


def test_the_two_phase_local_flow_composes_on_the_persons_machine_and_is_verified_here(engine, db, world, platform_server, other_server, monkeypatch):
    pair(engine, world)
    engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    monkeypatch.setattr(engine.router, "provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a local business must never be given a provider adapter")))
    prep = engine.local_prepare(world.A, world.viewer, HOURS)
    assert prep["done"] is False and re.fullmatch(r"[0-9a-f]{48}", prep["request_id"]) and prep["evidence_ids"] and prep["companion"]["runtime"] == "ollama"
    assert "[E1]" in prep["prompt"]["user"] and prep["prompt"]["schema"]["required"] == ["verdict", "claims"] and "answer" not in prep  # the evidence and the instructions: nothing is answered yet
    model_text = claims_reply()({"messages": [{"content": prep["prompt"]["user"]}]})
    done = engine.local_complete(world.A, world.viewer, prep["request_id"], model_text, None, 120, 40)
    res, ex = done["result"], done["result"]["execution"]
    assert ex["generated_by"] == "model" and ex["mode"] == "local" and ex["provider"] == "ollama" and ex["model"] == "qwen3:4b" and res["verified"] is True and res["answer"].startswith("We are open")
    assert completions(platform_server) == [] and completions(other_server) == []  # no provider anywhere saw the evidence
    row = db.su("select mode, tokens_in, tokens_out, cost, status from public.usage_records where business_id = %s and kind = 'llm_call'", (world.A,))[0]
    assert row[0] == "local" and (row[1], row[2]) == (120, 40) and row[3] is None and row[4] == "ok"
    with pytest.raises(svc.ServiceError) as e:
        engine.local_complete(world.A, world.viewer, prep["request_id"], model_text)
    assert e.value.status == 410  # a request id works once


def test_what_comes_back_from_the_companion_is_as_untrusted_as_any_providers_text(engine, world):
    pair(engine, world)
    engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    for reply, kind in ((json.dumps({"verdict": "answered", "claims": [{"text": "We are open every day, all day, and we guarantee it.", "evidence": ["E1"]}], "missing": []}), "reasoning:unsupported_claims"),
                        (json.dumps({"verdict": "answered", "claims": [{"text": "We are open Monday to Saturday.", "evidence": ["E77"]}], "missing": []}), "reasoning:unsupported_claims"),
                        ("not json at all", "reasoning:invalid_json"), (json.dumps({"verdict": "maybe"}), "reasoning:schema")):
        prep = engine.local_prepare(world.A, world.viewer, HOURS)
        ex = engine.local_complete(world.A, world.viewer, prep["request_id"], reply)["result"]["execution"]
        assert ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"] == kind and ex["mode"] == "local", reply
    prep = engine.local_prepare(world.A, world.viewer, HOURS)
    ex = engine.local_complete(world.A, world.viewer, prep["request_id"], None, "timeout")["result"]["execution"]  # the companion could not answer: a provider-kind failure, reported by the client
    assert ex["failure_kind"] == "provider:timeout" and ex["generated_by"] == "deterministic_fallback"


def test_a_local_request_belongs_to_the_person_and_business_it_was_made_for(engine, db, world):
    pair(engine, world)
    engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    prep = engine.local_prepare(world.A, world.viewer, HOURS)
    rid = prep["request_id"]
    good = claims_reply()({"messages": [{"content": prep["prompt"]["user"]}]})
    with pytest.raises(svc.ServiceError) as e:
        engine.local_complete(world.A, world.editor, rid, good)  # another person of the same business
    assert e.value.status == 403
    with pytest.raises(svc.ServiceError) as e:
        engine.local_complete(world.B, world.other, rid, good)  # another business
    assert e.value.status == 403
    assert engine.local_complete(world.A, world.viewer, rid, good)["result"]["execution"]["generated_by"] == "model"  # the owner of the request can still finish it
    for bad, status in (("short", 400), (None, 400), (12, 400), ("g" * 48, 400)):
        with pytest.raises(svc.ServiceError) as e:
            engine.local_complete(world.A, world.viewer, bad, good)
        assert e.value.status == status
    prep = engine.local_prepare(world.A, world.viewer, HOURS)
    for kw, status in (({"failure": "oops"}, 400), ({"text": "x" * 200_001}, 400), ({"text": 5}, 400)):
        with pytest.raises(svc.ServiceError) as e:
            engine.local_complete(world.A, world.viewer, prep["request_id"], **{"text": good, **kw})
        assert e.value.status == status


def test_local_requests_expire_and_are_bounded(engine, world):
    pair(engine, world)
    engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    engine.pending_ttl = 0.05
    prep = engine.local_prepare(world.A, world.viewer, HOURS)
    time.sleep(0.1)
    with pytest.raises(svc.ServiceError) as e:
        engine.local_complete(world.A, world.viewer, prep["request_id"], claims_reply()({"messages": [{"content": prep["prompt"]["user"]}]}))
    assert e.value.status == 410
    engine.pending_ttl = 60.0
    for _ in range(20):
        engine.local_prepare(world.A, world.viewer, HOURS)
    with pytest.raises(svc.ServiceError) as e:
        engine.local_prepare(world.A, world.viewer, HOURS)
    assert e.value.status == 429  # a business cannot pile up work waiting on its own machine


def test_local_prepare_refuses_a_business_that_is_not_in_local_mode_and_needs_no_model_when_there_is_nothing_to_compose(engine, world):
    with pytest.raises(svc.ServiceError) as e:
        engine.local_prepare(world.A, world.viewer, HOURS)
    assert e.value.status == 409
    pair(engine, world)
    engine.ai_settings_save(world.A, world.owner, {"mode": "local"})
    out = engine.local_prepare(world.A, world.viewer, "Do you sell submarines?")  # nothing in the knowledge answers it: no model is asked
    assert out["done"] is True and out["result"]["execution"]["generated_by"] == "deterministic" and out["result"]["execution"]["inference_attempted"] is False and "request_id" not in out
    with pytest.raises(svc.ServiceError) as e:
        engine.local_prepare(world.B, world.other, HOURS)
    assert e.value.status == 409  # B is deterministic


# ================================================================== observability
def test_every_answer_has_a_correlation_id_that_follows_it_into_the_usage_record_and_the_log(engine, db, world, caplog):
    with caplog.at_level("INFO", logger="faqengine.hosted"):
        r = engine.respond(world.A, world.viewer, HOURS, None, "trace-me-0123456789")
    ex = r["result"]["execution"]
    assert ex["correlation_id"] == "trace-me-0123456789"
    assert db.su("select correlation_id from public.usage_records where business_id = %s and kind = 'question' order by created_at desc limit 1", (world.A,))[0][0] == "trace-me-0123456789"
    assert any(getattr(rec, "fields", {}).get("correlation_id") == "trace-me-0123456789" for rec in caplog.records)
    for bad in ("short", "has space in it 123", "x" * 65, "line\nbreak-123456", None, 5):
        again = engine.respond(world.A, world.viewer, HOURS, None, bad)["result"]["execution"]["correlation_id"]
        assert re.fullmatch(r"[0-9a-f]{32}", again), bad  # a malformed id is replaced, never trusted into a log


def test_the_logs_of_a_request_hold_ids_and_kinds_and_never_what_was_said_or_a_key(engine, db, world, platform_server, caplog):
    platform_ready(engine, db, world)
    with caplog.at_level("DEBUG", logger="faqengine"):
        ask(engine, world.A, world.viewer, "What are your opening hours? My card is 4111 1111 1111 1111")
    lines = [telemetry.JsonFormatter().format(r) for r in caplog.records]
    text = "\n".join(lines)
    assert '"event": "respond"' in text and "4111" not in text and "opening hours" not in text.lower() and PLATFORM_KEY not in text and "Bearer" not in text
    for line in lines:
        json.loads(line)  # every line is one JSON object


def test_metrics_and_alerts_see_the_traffic_and_the_failures(engine, db, world, platform_server):
    platform_ready(engine, db, world)
    ask(engine, world.A, world.viewer)
    platform_server.status = 500
    for _ in range(3):
        ask(engine, world.A, world.viewer)
    m = engine.metrics()
    c = m["telemetry"]["counters"]
    assert c["request"] >= 4 and c["provider_call"] >= 4 and c["provider_failure"] >= 3 and c["generated_by_model"] >= 1 and "provider_ms" in m["telemetry"]["latency"] and "request_ms" in m["telemetry"]["latency"]
    firing = {a["name"] for a in engine.alerts_get()["firing"]}
    assert "circuit_open" in firing and "provider_failing" in firing
    assert all(set(a) >= {"name", "severity", "says", "firing"} for a in engine.alerts_get()["rules"])


def test_readiness_reports_booleans_only(engine, monkeypatch):
    r = engine.ready()
    assert r == {"ready": True, "checks": {"database": True, "schema": True, "embedding_model": True}}
    monkeypatch.setattr(engine.store, "_one", lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("connection to postgres://user:secret-password@db.example failed")))
    bad = engine.ready()
    assert bad["ready"] is False and "secret-password" not in json.dumps(bad) and all(isinstance(v, bool) for v in bad["checks"].values())


def test_security_events_are_counted(engine, world):
    before = engine.tele.counters["security_denied"]
    for call in (lambda: engine.respond(world.A, world.other, HOURS), lambda: engine.ai_settings_save(world.A, world.viewer, {"mode": "deterministic"})):
        with pytest.raises(svc.ServiceError):
            call()
    assert engine.tele.counters["security_denied"] == before + 2


def test_a_limit_hit_is_not_a_server_error(engine, db, world):
    platform_ready(engine, db, world, requests=0)
    r = engine.respond(world.A, world.viewer, HOURS)
    assert r["result"]["execution"]["failure_kind"] == "limit:quota_requests" and engine.tele.counters["quota_refused"] >= 1 and engine.tele.counters["request_error"] == 0
