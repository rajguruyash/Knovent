"""The language-model connection: validation, errors, credentials that never leak, and the limits on where a hosted deployment may connect."""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm as L  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402

KEY = "sk-test-key-123456"
SETUPS = [("openai", "/v1"), ("anthropic", "/v1"), ("gemini", "/v1beta"), ("openai_compatible", "/v1")]


@pytest.fixture()
def provider():
    p = FakeProvider(key=KEY)
    yield p
    p.close()


def cfg(provider_name, p, prefix, **kw):
    return L.LLMConfig(enabled=True, provider=provider_name, model=kw.pop("model", "chat-small"), api_key=kw.pop("api_key", KEY), base_url=p.url(prefix), **kw)


@pytest.mark.parametrize("name,prefix", SETUPS)
def test_a_valid_key_and_model_validate_and_models_are_listed_without_embedding_models(provider, name, prefix):
    r = L.validate(cfg(name, provider, prefix))
    assert r["ok"] is True and r["checked"] == ["key", "model"] and r["models"] == ["chat-large", "chat-small"]  # the embedding model is not a chat model
    assert KEY not in repr(r)


@pytest.mark.parametrize("name,prefix", SETUPS)
def test_a_wrong_key_is_reported_as_invalid_and_never_echoed(provider, name, prefix):
    r = L.validate(cfg(name, provider, prefix, api_key="sk-wrong-key-999999"))
    assert r["ok"] is False and r["code"] == "invalid_key" and "sk-wrong-key-999999" not in repr(r) and KEY not in repr(r)


def test_a_missing_key_or_provider_or_address_is_a_clear_message_not_a_crash():
    assert L.validate(L.LLMConfig(provider="openai"))["code"] == "missing_key"
    assert L.validate(L.LLMConfig(provider="nope"))["code"] == "not_configured"
    assert L.validate(L.LLMConfig(provider="openai_compatible"))["code"] == "not_configured"


@pytest.mark.parametrize("status,code", [(401, "invalid_key"), (403, "forbidden"), (404, "model_not_found"), (429, "rate_limited"), (500, "unavailable"), (503, "unavailable")])
def test_provider_failures_get_a_code_a_screen_can_act_on(provider, status, code):
    provider.status = status
    r = L.validate(cfg("openai", provider, "/v1"))
    assert r["ok"] is False and r["code"] == code and KEY not in repr(r)


def test_a_model_the_key_cannot_use_is_named_and_alternatives_offered(provider):
    r = L.validate(cfg("openai", provider, "/v1", model="gpt-nonexistent"))
    assert r["code"] == "model_not_found" and "chat-small" in r["models"]
    assert L.validate(cfg("openai", provider, "/v1", model=""))["message"] == "The key works. Choose a model."


def test_a_slow_provider_times_out_and_a_redirect_is_not_followed(provider):
    provider.delay = 1.0
    r = L.validate(cfg("openai", provider, "/v1", timeout=0.2))
    assert r["code"] == "timeout"
    provider.delay, provider.redirect = 0, True
    assert L.validate(cfg("openai", provider, "/v1"))["code"] == "bad_response" and len(provider.requests) >= 1


@pytest.mark.parametrize("name,prefix,where", [("openai", "/v1", "authorization"), ("anthropic", "/v1", "x-api-key"), ("gemini", "/v1beta", "x-goog-api-key")])
def test_the_key_travels_in_a_header_only_never_in_the_address_or_body(provider, name, prefix, where):
    c = cfg(name, provider, prefix)
    text = L.LLM(c).complete("system rules", "the question")
    assert text == "OK"
    req = provider.requests[-1]
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert KEY in headers[where] and KEY not in req["path"] and KEY not in repr(req["body"])


@pytest.mark.parametrize("name,prefix", SETUPS)
def test_complete_reads_each_providers_answer_and_usage(provider, name, prefix):
    provider.reply = "Hello there"
    m = L.LLM(cfg(name, provider, prefix, price_per_1k_in=1.0, price_per_1k_out=2.0))
    assert m.complete("s", "u") == "Hello there"
    u = m.usage()
    assert u["calls"] == 1 and u["tokens_in"] == 11 and u["tokens_out"] == 7 and u["cost"] == pytest.approx(11 / 1000 * 1.0 + 7 / 1000 * 2.0)
    assert L.LLM(cfg(name, provider, prefix)).usage()["cost"] is None  # no price configured: no cost invented


def test_failures_and_logs_never_contain_the_key(provider, caplog):
    provider.status = 401
    m = L.LLM(cfg("openai", provider, "/v1"))
    with caplog.at_level(logging.DEBUG, logger="faqengine.llm"):
        with pytest.raises(L.LLMError) as e:
            m.complete("s", "u")
    assert e.value.code == "invalid_key" and KEY not in e.value.message and KEY not in caplog.text and "provider=openai" in caplog.text
    assert KEY not in repr(cfg("openai", provider, "/v1")) and KEY not in str(cfg("openai", provider, "/v1").public())
    assert L.redact(f"Authorization: Bearer {KEY} and api_key={KEY}", KEY).count("[key hidden]") == 2


def test_the_public_form_reports_the_key_only_as_present_and_says_where_questions_go():
    c = L.LLMConfig(enabled=True, provider="openai", model="m", api_key=KEY)
    pub = c.public()
    assert pub["has_key"] is True and "api_key" not in pub and "api.openai.com" in pub["disclosure"] and "Nothing else is sent" in pub["disclosure"]
    local = L.LLMConfig(enabled=True, provider="openai_compatible", model="m", base_url="http://localhost:11434/v1").public()
    assert "on this computer" in local["disclosure"]


def test_an_unconfigured_or_disabled_connection_refuses_to_call_out(provider):
    assert L.LLM(L.LLMConfig(provider="openai", model="m", api_key=KEY, enabled=False)).available is False
    with pytest.raises(L.LLMError) as e:
        L.LLM(L.LLMConfig(provider="openai", model="", api_key=KEY, enabled=True, base_url=provider.url())).complete("s", "u")
    assert e.value.code == "not_configured" and provider.requests == []
    assert L.LLMConfig(provider="openai", model="m").problem() == "enter the provider's API key"
    assert L.LLM(L.LLMConfig(enabled=True, provider="openai", model="m", api_key=KEY, grounded=False)).can("grounded") is False


def test_a_hosted_deployment_connects_to_public_https_addresses_only(provider):
    for url in (provider.url(), "https://127.0.0.1/v1", "https://localhost/v1", "https://169.254.169.254/latest", "https://10.0.0.5/v1"):
        r = L.validate(L.LLMConfig(enabled=True, provider="openai_compatible", model="m", api_key=KEY, base_url=url), allow_private=False)
        assert r["ok"] is False and r["code"] == "blocked_endpoint", (url, r)
    assert provider.requests == []
    # and for the fixed providers a custom address is ignored: the key is never sent to a host the business typed
    c = L.LLMConfig(enabled=True, provider="openai", model="m", api_key=KEY, base_url=provider.url())
    assert L.LLM(c, allow_private=False)._base() == "https://api.openai.com/v1" and L.LLM(c, allow_private=True)._base() == provider.url()
