"""The inference layer (src/faqengine/inference): adapters over real HTTP against a stand-in provider, error classification, parameter negotiation with ONE model, bounded retries, the circuit breaker,
the registry and the catalogue. No real provider is called: what these prove is the adapters' behaviour, not any model's."""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_provider import FakeProvider  # noqa: E402

from faqengine.inference import catalogue, registry  # noqa: E402
from faqengine.inference.adapters import Anthropic, Gemini, OllamaNative, OpenAIStyle  # noqa: E402
from faqengine.inference.resilience import Breakers, CircuitBreaker, Resilient  # noqa: E402

KEY = "sk-adapter-key-424242"
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
ANSWER = json.dumps({"verdict": "answered"})


@pytest.fixture()
def server():
    p = FakeProvider(key=KEY, reply=ANSWER)
    yield p
    p.close()


def style(server, **kw):
    return OpenAIStyle(name="openai_compatible", model="chat-small", mode="byok", base_url=server.url(), key=KEY, allow_private=True, **kw)


def chats(server):
    return [r for r in server.requests if r["method"] == "POST"]


# ---------------------------------------------------------------- OpenAI-style
def test_a_reply_carries_text_tokens_latency_cost_and_the_model(server):
    a = style(server, price_in_per_m=1.0, price_out_per_m=2.0, structured="json_schema")
    r = a.complete("system", "user", SCHEMA)
    assert r.failure is None and json.loads(r.text) == {"verdict": "answered"} and (r.tokens_in, r.tokens_out) == (11, 7) and r.attempts == 1 and r.model == "chat-small" and r.seconds > 0
    assert r.cost == pytest.approx(11 / 1e6 * 1.0 + 7 / 1e6 * 2.0)
    sent = chats(server)[0]
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}" and sent["body"]["response_format"]["type"] == "json_schema" and sent["body"]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert sent["body"]["temperature"] == 0 and sent["body"]["model"] == "chat-small"


def test_no_price_means_no_cost_never_zero(server):
    r = style(server).complete("s", "u", SCHEMA)
    assert r.failure is None and r.cost is None


def test_a_model_not_known_to_support_a_schema_is_asked_for_json_in_words_only(server):
    style(server, structured="prompt").complete("s", "u", SCHEMA)
    assert "response_format" not in chats(server)[0]["body"]


def test_a_model_that_refuses_the_schema_is_asked_again_without_it_and_it_is_the_same_model(server):
    server.reject = {"response_format"}
    r = style(server, structured="auto").complete("s", "u", SCHEMA)
    sent = chats(server)
    assert r.failure is None and r.attempts == 2 and [x["body"]["model"] for x in sent] == ["chat-small", "chat-small"]
    assert "response_format" in sent[0]["body"] and "response_format" not in sent[1]["body"]


def test_parameters_a_model_does_not_take_are_negotiated_one_by_one(server):
    server.reject = {"temperature", "max_tokens"}
    r = style(server).complete("s", "u", None)
    sent = chats(server)
    assert r.failure is None and r.attempts == 3 and "temperature" not in sent[-1]["body"] and "max_completion_tokens" in sent[-1]["body"] and "max_tokens" not in sent[-1]["body"]


def test_negotiation_is_bounded_and_a_real_refusal_is_reported_as_one(server):
    server.status = 400
    r = style(server).complete("s", "u", SCHEMA)
    assert r.failure == "provider:bad_request" and r.attempts <= 4 and len(chats(server)) <= 4


def test_openrouter_is_told_to_route_only_to_endpoints_that_honour_the_parameters(server):
    a = OpenAIStyle(name="openrouter", model="chat-small", mode="platform", base_url=server.url(), key=KEY, allow_private=True, structured="json_schema", require_structured_routing=True)
    a.complete("s", "u", SCHEMA)
    assert chats(server)[0]["body"]["provider"] == {"require_parameters": True}
    b = OpenAIStyle(name="openrouter", model="chat-small", mode="platform", base_url=server.url(), key=KEY, allow_private=True, structured="prompt", require_structured_routing=True)
    b.complete("s", "u", SCHEMA)
    assert "provider" not in chats(server)[1]["body"]


def test_extra_body_from_the_catalogue_is_sent(server):
    style(server, extra_body={"reasoning": {"enabled": False}}).complete("s", "u", None)
    assert chats(server)[0]["body"]["reasoning"] == {"enabled": False}


# ---------------------------------------------------------------- Anthropic, Gemini, Ollama
def test_anthropic_gets_a_forced_tool_call_and_the_result_is_read_back_as_json(server):
    a = Anthropic(name="anthropic", model="claude-x", mode="byok", base_url=server.url(), key=KEY, allow_private=True)
    r = a.complete("s", "u", SCHEMA)
    body = chats(server)[0]["body"]
    assert r.failure is None and json.loads(r.text) == {"verdict": "answered"} and body["tool_choice"] == {"type": "tool", "name": "answer"} and body["tools"][0]["input_schema"] == SCHEMA
    assert chats(server)[0]["headers"]["x-api-key"] == KEY and "anthropic-version" in chats(server)[0]["headers"]


def test_anthropic_without_tools_falls_back_to_text_for_the_same_model(server):
    server.reject = {"tools"}
    r = Anthropic(name="anthropic", model="claude-x", mode="byok", base_url=server.url(), key=KEY, allow_private=True).complete("s", "u", SCHEMA)
    assert r.failure is None and r.attempts == 2 and "tools" not in chats(server)[1]["body"] and json.loads(r.text) == {"verdict": "answered"}


def test_gemini_gets_a_response_schema_and_the_key_in_a_header_not_the_address(server):
    g = Gemini(name="gemini", model="gem-1", mode="byok", base_url=server.url(), key=KEY, allow_private=True)
    r = g.complete("s", "u", SCHEMA)
    sent = chats(server)[0]
    assert r.failure is None and sent["body"]["generationConfig"]["responseSchema"] == SCHEMA and sent["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert sent["headers"]["x-goog-api-key"] == KEY and KEY not in sent["path"]


def test_gemini_refusing_the_schema_is_asked_again_in_prompt_mode(server):
    server.reject = {"responseSchema"}
    r = Gemini(name="gemini", model="gem-1", mode="byok", base_url=server.url(), key=KEY, allow_private=True).complete("s", "u", SCHEMA)
    assert r.failure is None and r.attempts == 2 and "responseSchema" not in chats(server)[1]["body"]["generationConfig"]


def test_ollama_native_sends_the_schema_as_format_and_reports_tokens(server):
    o = OllamaNative(name="ollama", model="chat-small", mode="local", base_url=server.url(""), allow_private=True)
    r = o.complete("s", "u", SCHEMA)
    assert r.failure is None and json.loads(r.text) == {"verdict": "answered"} and (r.tokens_in, r.tokens_out) == (11, 7)
    body = chats(server)[0]["body"]
    assert body["format"] == SCHEMA and body["stream"] is False and body["think"] is False and body["options"]["temperature"] == 0
    assert o.models() == sorted(server.models) and o.health()["ok"] is True


# ---------------------------------------------------------------- failures are classified, and nothing leaks
@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (404, "not_found"), (429, "rate_limited"), (500, "server"), (503, "server"), (400, "bad_request")])
def test_failures_are_provider_failures_with_a_kind(server, status, kind):
    server.status = status
    r = style(server).complete("s", "u", None)
    assert r.failure == f"provider:{kind}" and r.text == ""


def test_a_wrong_key_is_an_auth_failure_and_the_key_never_appears(server):
    a = OpenAIStyle(name="openai_compatible", model="chat-small", mode="byok", base_url=server.url(), key="sk-the-wrong-key-999999", allow_private=True)
    r = a.complete("s", "u", None)
    assert r.failure == "provider:auth" and "sk-the-wrong" not in json.dumps(r.__dict__) and "sk-the-wrong" not in repr(a)
    assert "sk-the-wrong" not in a.health()["detail"] and a.health()["kind"] == "auth"


def test_a_redirect_is_not_followed(server):
    server.redirect = True
    r = style(server).complete("s", "u", None)
    assert r.failure == "provider:server" and not [x for x in server.requests if "elsewhere" in x["path"]]


def test_a_slow_provider_times_out_and_an_unreachable_one_is_a_network_failure(server):
    server.delay = 1.5
    assert OpenAIStyle(name="x", model="chat-small", mode="byok", base_url=server.url(), key=KEY, allow_private=True, timeout=0.3).complete("s", "u", None).failure == "provider:timeout"
    dead = OpenAIStyle(name="x", model="m", mode="byok", base_url="http://127.0.0.1:1/v1", key=KEY, allow_private=True, timeout=1)
    assert dead.complete("s", "u", None).failure == "provider:network"


def test_a_hosted_deployment_will_not_talk_to_a_private_address_or_over_plain_http(server):
    r = OpenAIStyle(name="openai_compatible", model="chat-small", mode="byok", base_url=server.url(), key=KEY, allow_private=False).complete("s", "u", None)
    assert r.failure == "provider:blocked" and chats(server) == []  # nothing was sent to it
    r = OpenAIStyle(name="x", model="m", mode="byok", base_url="https://127.0.0.1:9/v1", key=KEY, allow_private=False).complete("s", "u", None)
    assert r.failure == "provider:blocked"


def test_a_cancelled_call_sends_nothing(server):
    stop = threading.Event()
    stop.set()
    r = style(server).complete("s", "u", None, cancel=stop)
    assert r.failure == "provider:cancelled" and r.attempts == 0 and chats(server) == []


def test_health_and_discovery_use_the_credential_and_check_the_configured_model(server):
    a = style(server)
    assert a.models() == ["chat-large", "chat-small", "text-embedding-3-small"] and a.health()["ok"] is True
    ghost = OpenAIStyle(name="x", model="not-listed", mode="byok", base_url=server.url(), key=KEY, allow_private=True)
    assert ghost.health()["ok"] is False and ghost.health()["kind"] == "not_found"


# ---------------------------------------------------------------- retries and the circuit breaker
class Script:
    """An adapter that answers from a list: a stand-in for provider outcomes, to test the policy without a network."""
    name, model, mode = "scripted", "m", "byok"

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def complete(self, system, user, schema=None, cancel=None):
        from faqengine.runtime.contracts import ProviderReply

        self.calls += 1
        o = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return ProviderReply(text="ok", tokens_in=1, tokens_out=1, model="m") if o is None else ProviderReply(failure=f"provider:{o}", model="m")

    def health(self):
        return {"ok": True}


def resilient(adapter, **kw):
    clock = {"t": 0.0}
    slept = []
    breaker = kw.pop("breaker", CircuitBreaker(failures=3, cooldown=10, clock=lambda: clock["t"]))
    r = Resilient(adapter, breaker, sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)), clock=lambda: clock["t"], jitter=lambda: 0.5, **kw)
    return r, slept, clock, breaker


def test_a_failure_worth_retrying_is_retried_on_the_same_adapter_after_a_bounded_wait():
    a = Script("server", None)
    r, slept, *_ = resilient(a, attempts=2, backoff=0.5)
    rep = r.complete("s", "u")
    assert rep.failure is None and a.calls == 2 and rep.attempts == 2 and slept == [0.5]


def test_a_failure_that_will_not_change_is_not_retried():
    for kind in ("auth", "not_found", "bad_request", "blocked", "quota"):
        a = Script(kind)
        r, slept, *_ = resilient(a, attempts=3)
        assert r.complete("s", "u").failure == f"provider:{kind}" and a.calls == 1 and slept == [], kind


def test_retries_are_bounded_by_attempts_and_by_the_deadline():
    a = Script("timeout")
    r, slept, *_ = resilient(a, attempts=3, backoff=1.0, deadline=100)
    assert r.complete("s", "u").failure == "provider:timeout" and a.calls == 3 and len(slept) == 2
    b = Script("timeout")
    r2, slept2, *_ = resilient(b, attempts=10, backoff=4.0, deadline=5)
    r2.complete("s", "u")
    assert b.calls < 10 and sum(slept2) < 5  # it stops when the next wait would pass the deadline


def test_the_breaker_opens_after_consecutive_provider_failures_and_then_calls_do_not_reach_the_provider():
    a = Script("server")
    r, _, clock, breaker = resilient(a, attempts=1)
    for _ in range(3):
        assert r.complete("s", "u").failure == "provider:server"
    assert breaker.state == "open" and a.calls == 3
    again = r.complete("s", "u")
    assert again.failure == "provider:circuit_open" and a.calls == 3  # the provider was not asked


def test_after_the_cooldown_one_probe_is_allowed_and_its_result_closes_or_reopens_the_circuit():
    a = Script("server", "server", "server", None)
    r, _, clock, breaker = resilient(a, attempts=1)
    for _ in range(3):
        r.complete("s", "u")
    clock["t"] += 11
    assert breaker.state == "half_open"
    assert breaker.allow() is True and breaker.allow() is False  # exactly one probe at a time
    breaker.record("provider:server")  # the probe failed: open again, for another cooldown
    assert breaker.state == "open"
    clock["t"] += 11
    assert r.complete("s", "u").failure is None and breaker.state == "closed"  # the probe worked


def test_a_callers_mistake_is_not_a_provider_outage():
    a = Script("auth")
    r, _, _, breaker = resilient(a, attempts=1)
    for _ in range(10):
        r.complete("s", "u")
    assert breaker.state == "closed" and a.calls == 10  # a bad key does not close the circuit for everyone


def test_a_success_resets_the_count():
    a = Script("server", "server", None, "server", "server")
    r, _, _, breaker = resilient(a, attempts=1)
    for _ in range(5):
        r.complete("s", "u")
    assert breaker.state == "closed"


def test_cancelling_stops_the_retries():
    stop = threading.Event()
    a = Script("server")
    clock = {"t": 0.0}
    r = Resilient(a, CircuitBreaker(clock=lambda: clock["t"]), attempts=5, backoff=0.1, sleep=lambda s: stop.set(), clock=lambda: clock["t"], cancel=stop)
    rep = r.complete("s", "u")
    assert rep.failure == "provider:cancelled" and a.calls == 1


def test_breakers_are_kept_apart_per_key():
    b = Breakers(failures=1, cooldown=5)
    b.get(("byok", "openai", "m", "business-a")).record("provider:server")
    assert b.get(("byok", "openai", "m", "business-a")).state == "open" and b.get(("byok", "openai", "m", "business-b")).state == "closed"
    assert b.states() == {"byok/openai/m": "open"}  # one business's open circuit shows as open for the provider; no business is named


# ---------------------------------------------------------------- the registry and the catalogue
def test_the_registry_knows_which_provider_can_serve_which_mode():
    assert registry.serves("cloudflare", "platform") and not registry.serves("cloudflare", "byok")
    assert registry.serves("openrouter", "platform") and registry.serves("openrouter", "byok")
    assert all(registry.serves(p, "byok") for p in ("openai", "anthropic", "gemini", "openai_compatible"))
    assert registry.serves("ollama", "local") and registry.serves("llamacpp", "local") and not registry.serves("ollama", "byok")
    assert registry.third_party("openai") and not registry.third_party("ollama") and registry.third_party("a-provider-nobody-heard-of")


def test_building_an_adapter_refuses_what_cannot_be_served():
    with pytest.raises(ValueError, match="unknown provider"):
        registry.build("nope", "m", "byok", key="k")
    with pytest.raises(ValueError, match="cannot be used in"):
        registry.build("openai", "m", "platform")
    with pytest.raises(ValueError, match="platform has no credential"):
        registry.build("cloudflare", "@cf/google/gemma-4-26b-a4b-it", "platform", platform=registry.PlatformCredentials())
    with pytest.raises(ValueError, match="needs the business's API key"):
        registry.build("anthropic", "claude-x", "byok")
    with pytest.raises(ValueError, match="server address"):
        registry.build("openai_compatible", "m", "byok")


def test_platform_credentials_come_from_the_operators_environment_and_are_never_shown():
    env = {"PLATFORM_CLOUDFLARE_ACCOUNT_ID": "acct-123", "PLATFORM_CLOUDFLARE_API_TOKEN": "cf-token-secret-xyz"}
    p = registry.PlatformCredentials.from_env(env)
    assert p.available("cloudflare") and not p.available("openrouter") and p.status()["PLATFORM_OPENROUTER_API_KEY"] == "MISSING"
    assert "cf-token-secret" not in repr(p) and "acct-123" not in repr(p) and p.status()["PLATFORM_CLOUDFLARE_API_TOKEN"] == "SET"
    a = registry.build("cloudflare", "@cf/google/gemma-4-26b-a4b-it", "platform", platform=p)
    assert a.base_url == "https://api.cloudflare.com/client/v4/accounts/acct-123/ai/v1" and "cf-token-secret" not in repr(a) and a.mode == "platform"
    assert not registry.PlatformCredentials.from_env({"CLOUDFLARE_API_TOKEN": "benchmark-token", "CLOUDFLARE_ACCOUNT_ID": "x"}).available("cloudflare")  # the benchmark's credentials are not production's


def test_a_model_from_the_catalogue_brings_its_verified_facts_and_an_unknown_one_brings_none():
    known = registry.build("openrouter", "qwen/qwen3.5-9b", "byok", key="k")
    assert known.structured == "json_schema" and known.extra_body == {"reasoning": {"enabled": False}} and known.price_in_per_m == 0.17 and known.require_structured_routing is True
    unknown = registry.build("openrouter", "some/new-model", "byok", key="k")
    assert unknown.price_in_per_m is None and unknown.extra_body == {} and unknown.structured == "auto"
    unverified = registry.build("cloudflare", "@cf/qwen/qwen3-30b-a3b-fp8", "platform", platform=registry.PlatformCredentials((("PLATFORM_CLOUDFLARE_ACCOUNT_ID", "a"), ("PLATFORM_CLOUDFLARE_API_TOKEN", "t"))))
    assert unverified.structured == "prompt"  # support not verified: asked for JSON in words
    assert registry.build("openai", "gpt", "byok", key="k", price_in_per_m=3.0, price_out_per_m=6.0).estimate_cost(1000, 1000) == pytest.approx(0.009)


def test_nothing_is_offered_by_the_platform_until_someone_releases_it_on_purpose():
    assert catalogue.platform_models() == []
    assert all(not m.platform_offered and m.verified_on and m.source for m in catalogue.entries().values())


def test_the_catalogue_refuses_a_model_offered_without_a_cost_model_or_a_tier(tmp_path, monkeypatch):
    def write(entry):
        f = tmp_path / "c.json"
        f.write_text(json.dumps({"models": [entry]}))
        monkeypatch.setenv("MODEL_CATALOGUE_FILE", str(f))
        return catalogue.entries()

    base = {"provider": "cloudflare", "id": "m", "label": "M", "structured": "unverified", "verified_on": "2026-11-02", "source": "https://x.invalid", "platform_offered": True}
    with pytest.raises(ValueError, match="no tier or no price"):
        write(base)
    with pytest.raises(ValueError, match="no tier or no price"):
        write({**base, "tier": "paid"})
    assert write({**base, "tier": "paid", "price_in_per_m": 1.0, "price_out_per_m": 2.0})[("cloudflare", "m")].cost(1_000_000, 1_000_000) == 3.0
    with pytest.raises(ValueError, match="structured"):
        write({**base, "structured": "always"})
    with pytest.raises(ValueError, match="missing"):
        write({"provider": "x", "id": "y"})
