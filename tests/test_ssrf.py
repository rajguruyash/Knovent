"""Server-side request forgery: what a business can make this service connect to. The only address a business ever supplies is a custom OpenAI-compatible server, and a source URL; both must reach
public addresses only, over https for providers, the address checked on what the name resolves to and that exact address connected to (so a name that changes its answer cannot slip a private one in)."""
import socket

import pytest

from faqengine.inference import registry
from faqengine.runtime import llm as L


def resolving(monkeypatch, *addresses):
    """Make every name resolve to the given addresses, as getaddrinfo would answer."""
    def fake(host, port, type=0, **_):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port, 0, 0) if ":" in a else (a, port)) for a in addresses]
    monkeypatch.setattr(L.socket, "getaddrinfo", fake)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.0.9", "172.31.255.255", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "192.0.0.1", "198.18.0.1", "224.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "ff02::1",
])
def test_an_address_that_is_not_public_is_refused(monkeypatch, address):
    resolving(monkeypatch, address)
    with pytest.raises(L.LLMError) as e:
        L._public_address("provider.example", 443)
    assert e.value.code == "blocked_endpoint"


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700::1111", "2001:4860:4860::8888"])
def test_a_public_address_is_the_one_connected_to(monkeypatch, address):
    resolving(monkeypatch, address)
    assert L._public_address("provider.example", 443) == address


def test_a_name_that_resolves_to_a_mix_is_refused_even_when_the_first_answer_is_public(monkeypatch):
    resolving(monkeypatch, "93.184.216.34", "10.0.0.5")  # DNS rebinding with a second record
    with pytest.raises(L.LLMError) as e:
        L._public_address("rebind.example", 443)
    assert e.value.code == "blocked_endpoint"
    resolving(monkeypatch, "93.184.216.34", "::ffff:127.0.0.1")
    with pytest.raises(L.LLMError):
        L._public_address("rebind.example", 443)


def test_the_address_that_was_checked_is_the_address_connected_to_not_a_second_lookup(monkeypatch):
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])  # a first lookup is public, a second would be private
    looked, connected = [], []

    def fake(host, port, type=0, **_):
        looked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in next(answers)]
    monkeypatch.setattr(L.socket, "getaddrinfo", fake)
    monkeypatch.setattr(L.socket, "create_connection", lambda addr, timeout=None: connected.append(addr) or (_ for _ in ()).throw(OSError("stop")))
    conn = L._PinnedTLS("rebind.example", 443, timeout=1)
    with pytest.raises(OSError):
        conn.connect()
    assert looked == ["rebind.example"] and connected == [("93.184.216.34", 443)]  # one lookup; the connection went to what it returned


def test_a_name_that_does_not_resolve_is_a_network_error_not_a_pass(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(L.socket, "getaddrinfo", boom)
    with pytest.raises(L.LLMError) as e:
        L._public_address("nope.invalid", 443)
    assert e.value.code == "network"


@pytest.mark.parametrize("url", [
    "http://provider.example/v1",  # plain http is never used from a hosted deployment
    "ftp://provider.example/", "file:///etc/passwd", "gopher://provider.example/", "javascript:alert(1)", "//provider.example/v1", "https:///v1", "provider.example/v1",
])
def test_a_hosted_request_needs_an_https_address(monkeypatch, url):
    resolving(monkeypatch, "93.184.216.34")
    with pytest.raises(L.LLMError) as e:
        L.request("POST", url, {}, {}, 1, allow_private=False)
    assert e.value.code in ("bad_request", "blocked_endpoint")


@pytest.mark.parametrize("url", ["https://127.0.0.1/v1", "https://[::1]/v1", "https://localhost/v1", "https://169.254.169.254/latest/meta-data", "https://10.0.0.5:8443/v1", "https://[::ffff:7f00:1]/v1", "https://0.0.0.0/v1"])
def test_a_hosted_request_to_a_private_literal_is_refused_before_anything_is_sent(url):
    with pytest.raises(L.LLMError) as e:
        L.request("POST", url, {"Authorization": "Bearer sk-secret"}, {"q": 1}, 1, allow_private=False)
    assert e.value.code == "blocked_endpoint"


def test_a_redirect_is_never_followed(monkeypatch):
    class Resp:
        status = 302
        def read(self, n=None):  # noqa: E301
            return b""

    class Conn:
        def __init__(self, *a, **k):
            self.sent = []
        def request(self, method, path, body=None, headers=None):  # noqa: E301
            self.sent.append(path)
        def getresponse(self):  # noqa: E301
            return Resp()
        def close(self):  # noqa: E301
            pass
    monkeypatch.setattr(L, "_PinnedTLS", Conn)
    with pytest.raises(L.LLMError, match="redirect"):
        L.request("POST", "https://provider.example/v1/chat", {}, {}, 1, allow_private=False)


# ---- what a business can influence: the adapter's base address
def test_a_business_cannot_point_a_fixed_provider_at_another_host():
    for provider, mode in (("openai", "byok"), ("anthropic", "byok"), ("gemini", "byok"), ("openrouter", "byok")):
        a = registry.build(provider, "m", mode, key="k", base_url="https://attacker.example/v1")
        assert "attacker" not in a.base_url and a.base_url.startswith("https://"), provider  # the address typed is ignored for a fixed provider


def test_a_custom_server_in_a_hosted_deployment_cannot_be_a_private_address(monkeypatch):
    resolving(monkeypatch, "10.0.0.5")
    reply = registry.build("openai_compatible", "m", "byok", key="k", base_url="https://internal.example/v1", allow_private=False).complete("system", "user", None)
    assert reply.failure == "provider:blocked" and reply.text == ""  # refused as a blocked endpoint: no request left this machine
    reply = registry.build("openai_compatible", "m", "byok", key="k", base_url="http://public.example/v1", allow_private=False).complete("s", "u", None)
    assert reply.failure == "provider:blocked"  # and plain http is not used either


def test_the_platform_credential_is_only_ever_sent_to_the_fixed_provider_address():
    p = registry.PlatformCredentials((("PLATFORM_OPENROUTER_API_KEY", "k-platform"),))
    a = registry.build("openrouter", "m", "platform", platform=p, base_url="https://attacker.example/v1")
    assert a.base_url == "https://openrouter.ai/api/v1"
    with pytest.raises(ValueError):
        registry.build("openai", "m", "platform", platform=p)  # a provider the platform does not serve in that mode
    with pytest.raises(ValueError):
        registry.build("openai_compatible", "m", "platform", platform=p, base_url="https://attacker.example/v1")  # and a custom server is never a platform route
