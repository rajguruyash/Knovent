"""Optional language-model onboarding in the Studio: validate, save, change, disable, and never let the key out."""
import json
import os
import stat
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_provider import FakeProvider  # noqa: E402

KEY = "sk-onboarding-key-424242"


@pytest.fixture()
def provider():
    p = FakeProvider(key=KEY)
    yield p
    p.close()


@pytest.fixture()
def pid(site):
    return site.j("POST", "/api/projects/example", {})["id"]


def post(site, path, body):
    try:
        return 200, json.loads(site.call("POST", path, body))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def everything_the_page_can_read(site, pid):
    """The raw text of every response that describes this project."""
    return "".join(site.call("GET", p).decode("utf-8", "ignore") for p in (f"/api/projects/{pid}", f"/api/projects/{pid}?light=1", f"/api/projects/{pid}/llm", "/api/overview", "/api/llm/providers"))


def test_the_providers_are_listed_and_a_new_project_has_not_been_asked_yet(site, pid):
    keys = {p["key"] for p in site.j("GET", "/api/llm/providers")["providers"]}
    assert keys == {"openai", "anthropic", "gemini", "openrouter", "openai_compatible"}  # OpenRouter is offered for a business's own key too (BYOK)
    llm = site.j("GET", f"/api/projects/{pid}")["llm"]
    assert llm["enabled"] is False and llm["onboarded"] is False and llm["has_key"] is False and llm["usable"] is False and "api_key" not in llm


def test_continuing_in_local_only_mode_answers_the_question_without_a_provider(site, pid):
    r = site.j("POST", f"/api/projects/{pid}/llm/skip", {})
    assert r["onboarded"] is True and r["enabled"] is False and r["provider"] == ""


def test_a_wrong_key_is_explained_and_can_be_retried_or_skipped(site, pid, provider):
    code, r = post(site, f"/api/projects/{pid}/llm/validate", {"provider": "openai_compatible", "base_url": provider.url(), "api_key": "sk-not-the-right-key-1"})
    assert code == 200 and r["ok"] is False and r["code"] == "invalid_key" and "sk-not-the-right-key-1" not in json.dumps(r)
    code, r = post(site, f"/api/projects/{pid}/llm/validate", {"provider": "openai_compatible", "base_url": provider.url(), "api_key": KEY})
    assert r["ok"] is True and r["models"] == ["chat-large", "chat-small"] and KEY not in json.dumps(r)
    assert site.j("GET", f"/api/projects/{pid}/llm")["has_key"] is False  # validating saves nothing
    assert site.j("POST", f"/api/projects/{pid}/llm/skip", {})["enabled"] is False  # and a failed attempt never blocks onboarding


def test_missing_key_and_incomplete_settings_are_refused_with_a_message(site, pid):
    code, r = post(site, f"/api/projects/{pid}/llm", {"provider": "openai", "enabled": True})
    assert code == 400 and "API key" in r["error"]
    code, r = post(site, f"/api/projects/{pid}/llm", {"provider": "openai", "api_key": KEY, "enabled": True})
    assert code == 400 and "model" in r["error"]
    code, r = post(site, f"/api/projects/{pid}/llm", {"provider": "made-up"})
    assert code == 400
    code, r = post(site, f"/api/projects/{pid}/llm", {"provider": "openai_compatible", "base_url": "ftp://x", "api_key": KEY})
    assert code == 400


def test_saving_the_key_keeps_it_on_the_server_and_out_of_every_response(site, pid, provider):
    saved = site.j("POST", f"/api/projects/{pid}/llm", {"provider": "openai_compatible", "base_url": provider.url(), "api_key": KEY, "model": "chat-small", "enabled": True})
    assert saved["enabled"] is True and saved["has_key"] is True and saved["usable"] is True and saved["onboarded"] is True and "api_key" not in saved
    assert KEY not in saved["disclosure"] and "Each customer question" in saved["disclosure"] or "this computer" in saved["disclosure"]
    assert KEY not in everything_the_page_can_read(site, pid)
    path = site.store.dir(pid) / "llm.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and json.loads(path.read_text())["api_key"] == KEY  # readable by the owner only
    from faqengine.studio.llm_settings import LLMSettings

    conn = LLMSettings(site.store.dir(pid)).connection()
    assert conn is not None and conn.available and conn.complete("rules", "hello") == "OK"


def test_changing_the_model_keeps_the_key_and_changing_the_provider_drops_it(site, pid, provider):
    site.j("POST", f"/api/projects/{pid}/llm", {"provider": "openai_compatible", "base_url": provider.url(), "api_key": KEY, "model": "chat-small", "enabled": True})
    again = site.j("POST", f"/api/projects/{pid}/llm", {"model": "chat-large"})
    assert again["model"] == "chat-large" and again["has_key"] is True and again["enabled"] is True
    other = site.j("POST", f"/api/projects/{pid}/llm", {"provider": "anthropic", "enabled": False})
    assert other["provider"] == "anthropic" and other["has_key"] is False and other["model"] == "" and other["enabled"] is False  # a key is never carried to another provider


def test_it_can_be_turned_off_and_on_again_and_each_business_keeps_its_own(site, pid, provider):
    other_pid = site.j("POST", "/api/projects/example", {})["id"]
    site.j("POST", f"/api/projects/{pid}/llm", {"provider": "openai_compatible", "base_url": provider.url(), "api_key": KEY, "model": "chat-small", "enabled": True})
    assert site.j("GET", f"/api/projects/{other_pid}/llm")["has_key"] is False  # the other business has nothing
    off = site.j("POST", f"/api/projects/{pid}/llm", {"enabled": False})
    assert off["enabled"] is False and off["has_key"] is True and off["usable"] is False
    from faqengine.studio.llm_settings import LLMSettings

    assert LLMSettings(site.store.dir(pid)).connection() is None  # off means off: nothing will be sent
    assert site.j("POST", f"/api/projects/{pid}/llm", {"enabled": True})["usable"] is True
    gone = site.j("POST", f"/api/projects/{pid}/llm/remove", {})
    assert gone["has_key"] is False and not (site.store.dir(pid) / "llm.json").exists()


def test_a_page_on_another_site_cannot_read_or_change_the_settings(site, pid):
    from faqengine.studio import server

    h = server.Handler.__new__(server.Handler)
    h.port, h.headers = 8765, {"Host": "127.0.0.1:8765", "Origin": "https://evil.example"}
    assert h._allowed() is False


def test_nothing_in_a_package_can_be_a_credential(tmp_path):
    from faqengine import export

    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "engine.json").write_text("{}")
    export.assert_no_secrets(tmp_path)
    (tmp_path / "model" / "oops.json").write_text('{"api_key": "sk-abcdefghijklmnopqrstuv"}')
    with pytest.raises(RuntimeError, match="credential"):
        export.assert_no_secrets(tmp_path)
    (tmp_path / "model" / "oops.json").unlink()
    (tmp_path / "llm.json").write_text("{}")
    with pytest.raises(RuntimeError, match="settings"):
        export.assert_no_secrets(tmp_path)
