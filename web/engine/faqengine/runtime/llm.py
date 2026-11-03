"""THE LANGUAGE-MODEL CONNECTION. An optional enhancement above retrieval and evidence: it can reword an answer that is already established, combine passages that already
support one, and suggest other ways to search. It cannot decide that anything is supported, and nothing it writes is used until verify.py has checked it. Without it, every
other part of the engine works as before.

Providers: openai · anthropic · gemini · openai_compatible (any server that speaks the OpenAI chat API: Ollama, LM Studio, vLLM, a company gateway). Standard library only.

Credentials: the key lives in an `LLMConfig`, is never part of its repr, its `public()` form, an error message or a log line (`redact`), and is sent only in a request header to
the provider's host. Requests are made without following redirects. For a hosted deployment (`allow_private=False`) the endpoint must resolve to public addresses only, and
that is checked on the address actually connected to.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field

log = logging.getLogger("faqengine.llm")

PROVIDERS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "key": True, "custom_url": False},
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com/v1", "key": True, "custom_url": False},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "key": True, "custom_url": False},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "key": True, "custom_url": False},
    "openai_compatible": {"label": "OpenAI-compatible server (Ollama, LM Studio, vLLM, a gateway)", "base_url": "", "key": False, "custom_url": True},
}
MAX_RESPONSE_BYTES = 2_000_000
NON_CHAT = re.compile(r"embed|whisper|tts|dall-e|moderation|image|audio|realtime|transcribe|speech|vision-preview|davinci|babbage|search|similarity", re.I)


class LLMError(Exception):
    """A failure with a code a screen can act on: missing_key · invalid_key · forbidden · rate_limited · quota · model_not_found · bad_request · unavailable · timeout ·
    network · bad_response · blocked_endpoint · not_configured. The message never contains the key."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def redact(text: object, *secrets: str) -> str:
    out = str(text)
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "[key hidden]")
    return re.sub(r"(?i)(bearer\s+|x-api-key[:=]\s*|api[_-]?key[:=]\s*)[A-Za-z0-9._\-]{8,}", r"\1[key hidden]", out)


@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    polish: bool = True  # reword a stored FAQ answer that is already sufficient
    grounded: bool = True  # combine passages that already support the answer
    assist: bool = True  # suggest other searches when the first retrieval found too little
    timeout: float = 20.0
    max_tokens: int = 400
    price_per_1k_in: float | None = None  # optional, to report a cost: never guessed, because prices change
    price_per_1k_out: float | None = None

    def public(self) -> dict:
        """What a screen or an API response may show: everything except the key (which is reported only as present or not)."""
        return {"enabled": self.enabled, "provider": self.provider, "model": self.model, "base_url": self.base_url, "has_key": bool(self.api_key), "polish": self.polish,
                "grounded": self.grounded, "assist": self.assist, "label": PROVIDERS.get(self.provider, {}).get("label", ""), "disclosure": disclosure(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "LLMConfig":
        keep = {k: d[k] for k in ("enabled", "provider", "model", "api_key", "base_url", "polish", "grounded", "assist", "timeout", "max_tokens", "price_per_1k_in", "price_per_1k_out") if k in d}
        return cls(**keep)

    def problem(self) -> str | None:
        """Why this configuration cannot be used, or None."""
        spec = PROVIDERS.get(self.provider)
        if spec is None:
            return "choose a provider"
        if spec["key"] and not self.api_key:
            return "enter the provider's API key"
        if spec["custom_url"] and not self.base_url:
            return "enter the server's address"
        if not self.model:
            return "choose a model"
        return None


def disclosure(cfg: LLMConfig) -> str:
    """What is sent where, in words a business owner can read before turning this on."""
    spec = PROVIDERS.get(cfg.provider)
    if spec is None:
        return "With a language model turned on, a customer's question and the FAQ answers or document passages found for it are sent to the provider you choose."
    host = urllib.parse.urlparse(cfg.base_url or spec["base_url"]).hostname or "the provider"
    if host in ("localhost", "127.0.0.1", "::1"):
        return "The model runs on this computer: questions and passages are sent to it and nowhere else."
    return (f"Each customer question that needs a model, and the FAQ answer or the few passages found for it, are sent to {spec['label']} ({host}) to be worded or searched. "
            "Nothing else is sent: not the rest of your documents, not your API key's owner details, not other customers' questions. Their privacy terms apply to what they receive. "
            "Turn this off to keep everything on your own system.")


# ---- the network, kept small and strict
def _public_address(host: str, port: int) -> str:
    """The address to connect to, which must be a public one (checked on what the name resolves to, and that exact address is then connected to)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise LLMError("network", f"could not find {host}: {e}") from None
    for fam, _, _, _, addr in infos:
        ip = ipaddress.ip_address(addr[0])
        if not ip.is_global or ip.is_multicast:
            raise LLMError("blocked_endpoint", f"{host} is not a public address, so it cannot be used here")
    return infos[0][4][0]


class _Pinned(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.create_connection((_public_address(self.host, self.port), self.port), self.timeout)


class _PinnedTLS(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection((_public_address(self.host, self.port), self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def request(method: str, url: str, headers: dict, body: dict | None, timeout: float, allow_private: bool = True) -> tuple[int, dict]:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise LLMError("bad_request", "the address must start with http:// or https://")
    port = u.port or (443 if u.scheme == "https" else 80)
    if allow_private:
        conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.hostname, port, timeout=timeout, **({"context": ssl.create_default_context()} if u.scheme == "https" else {}))
    else:
        if u.scheme != "https":
            raise LLMError("blocked_endpoint", "a hosted deployment only talks to providers over https")
        conn = _PinnedTLS(u.hostname, port, timeout=timeout, context=ssl.create_default_context())
    data = json.dumps(body).encode() if body is not None else None
    try:
        conn.request(method, (u.path or "/") + (f"?{u.query}" if u.query else ""), body=data, headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
        resp = conn.getresponse()
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except LLMError:
        raise
    except (socket.timeout, TimeoutError):
        raise LLMError("timeout", "the provider did not answer in time") from None
    except (OSError, http.client.HTTPException) as e:
        raise LLMError("network", f"could not reach the provider ({type(e).__name__})") from None
    finally:
        conn.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LLMError("bad_response", "the provider's answer was too large")
    if 300 <= resp.status < 400:
        raise LLMError("bad_response", "the provider redirected the request; redirects are not followed")
    try:
        return resp.status, (json.loads(raw) if raw else {})
    except ValueError:
        return resp.status, {"_raw": raw[:300].decode("utf-8", "replace")}


def _error_for(status: int, payload: dict, key: str) -> LLMError:
    err = payload.get("error") if isinstance(payload, dict) else None
    detail = redact((err.get("message") if isinstance(err, dict) else err) or payload.get("message") or payload.get("_raw") or "", key)[:200] if isinstance(payload, dict) else ""
    low = detail.lower()
    if status == 401 or (status in (400, 403) and ("api key not valid" in low or "invalid api key" in low or "invalid x-api-key" in low or "incorrect api key" in low)):  # Gemini says 400 for a bad key
        return LLMError("invalid_key", "the provider rejected the API key" + (f" ({detail})" if detail else ""))
    if status == 403:
        return LLMError("forbidden", "the key is valid but is not allowed to do this" + (f" ({detail})" if detail else ""))
    if status == 404:
        return LLMError("model_not_found", "the provider does not know that model or address" + (f" ({detail})" if detail else ""))
    if status == 429:
        return LLMError("rate_limited" if "quota" not in detail.lower() and "billing" not in detail.lower() else "quota", "the provider is limiting requests or the account is out of quota" + (f" ({detail})" if detail else ""))
    if status == 400:
        return LLMError("bad_request", "the provider refused the request" + (f" ({detail})" if detail else ""))
    if status >= 500:
        return LLMError("unavailable", f"the provider is not available right now (HTTP {status})")
    return LLMError("bad_response", f"unexpected answer from the provider (HTTP {status})")


class LLM:
    """A configured connection. `complete(system, user)` returns text or raises LLMError; it keeps the totals of what it used. Cheap to create."""

    def __init__(self, cfg: LLMConfig, allow_private: bool = True):
        self.cfg, self.allow_private = cfg, allow_private
        self.calls = self.tokens_in = self.tokens_out = 0
        self.seconds = 0.0
        self.last_error: LLMError | None = None

    @property
    def available(self) -> bool:
        return self.cfg.enabled and self.cfg.problem() is None

    def can(self, capability: str) -> bool:
        return self.available and bool(getattr(self.cfg, capability, False))

    def _base(self) -> str:
        spec = PROVIDERS[self.cfg.provider]
        # a custom address is honoured for a server the business runs itself, and (so that tests and local setups can point at a stand-in) when private addresses are allowed;
        # a hosted deployment talks to the real provider addresses only
        return (self.cfg.base_url if self.cfg.base_url and (spec["custom_url"] or self.allow_private) else spec["base_url"]).rstrip("/")

    def _headers(self) -> dict:
        k, p = self.cfg.api_key, self.cfg.provider
        if p == "anthropic":
            return {"x-api-key": k, "anthropic-version": "2023-06-01"}
        if p == "gemini":
            return {"x-goog-api-key": k}  # in a header, not the address: addresses end up in logs
        return {"Authorization": f"Bearer {k}"} if k else {}

    def models(self) -> list[str]:
        """The chat models this key can use (a call that costs nothing). Raises LLMError."""
        if self.cfg.provider not in PROVIDERS:
            raise LLMError("not_configured", "choose a provider")
        spec = PROVIDERS[self.cfg.provider]
        if spec["key"] and not self.cfg.api_key:
            raise LLMError("missing_key", "enter the provider's API key")
        status, data = request("GET", f"{self._base()}/models" + ("?pageSize=200" if self.cfg.provider == "gemini" else ""), self._headers(), None, self.cfg.timeout, self.allow_private)
        if status != 200:
            raise _error_for(status, data, self.cfg.api_key)
        if self.cfg.provider == "gemini":
            names = [m["name"].split("/", 1)[-1] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        else:
            names = [m.get("id", "") for m in data.get("data", [])]
        return sorted({n for n in names if n and not NON_CHAT.search(n)})

    def complete(self, system: str, user: str, max_tokens: int | None = None) -> str:
        why = self.cfg.problem()
        if not self.cfg.enabled or why:
            raise LLMError("not_configured", why or "the language model is turned off")
        p, m, n = self.cfg.provider, self.cfg.model, max_tokens or self.cfg.max_tokens
        started = time.perf_counter()
        try:
            if p == "anthropic":
                body = {"model": m, "max_tokens": n, "system": system, "messages": [{"role": "user", "content": user}]}
                url = f"{self._base()}/messages"
            elif p == "gemini":
                body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": n}}
                url = f"{self._base()}/models/{urllib.parse.quote(m, safe='')}:generateContent"
            else:
                body = {"model": m, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0, "max_tokens": n}
                url = f"{self._base()}/chat/completions"
            status, data = request("POST", url, self._headers(), body, self.cfg.timeout, self.allow_private)
            if status == 400 and "temperature" in json.dumps(data).lower() and "temperature" in body:  # some models accept no temperature: ask again without
                body.pop("temperature")
                status, data = request("POST", url, self._headers(), body, self.cfg.timeout, self.allow_private)
            if status != 200:
                raise _error_for(status, data, self.cfg.api_key)
            text, tin, tout = self._parse(data)
        except LLMError as e:
            self.last_error = LLMError(e.code, redact(e.message, self.cfg.api_key))
            log.warning("language model call failed: provider=%s model=%s code=%s", p, m, e.code)  # never the request, never the key
            raise self.last_error from None
        finally:
            self.seconds += time.perf_counter() - started
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout
        return text

    def _parse(self, data: dict) -> tuple[str, int, int]:
        try:
            if self.cfg.provider == "anthropic":
                text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
                u = data.get("usage", {})
                return text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
            if self.cfg.provider == "gemini":
                text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
                u = data.get("usageMetadata", {})
                return text, int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))
            u = data.get("usage", {})
            return data["choices"][0]["message"]["content"] or "", int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("bad_response", "the provider's answer was not in the expected form") from None

    def __call__(self, prompt: str) -> str:
        """The plain `llm(prompt) -> text` form older callers use."""
        return self.complete("You answer strictly from the material you are given.", prompt)

    def usage(self) -> dict:
        cost = None
        c = self.cfg
        if c.price_per_1k_in is not None and c.price_per_1k_out is not None:
            cost = self.tokens_in / 1000 * c.price_per_1k_in + self.tokens_out / 1000 * c.price_per_1k_out
        return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "seconds": round(self.seconds, 3), "cost": cost,
                "cost_note": None if cost is not None else "no price was configured, so no cost is reported"}


def validate(cfg: LLMConfig, allow_private: bool = True) -> dict:
    """Checks a configuration without saving anything: the key (by listing the models it can use, which costs nothing) and, if a model is chosen, that the model answers
    (one very small request). Never raises. {ok, code, message, models, checked}. The key is never part of the result."""
    llm = LLM(LLMConfig.from_dict({**cfg.__dict__, "enabled": True}), allow_private)
    out = {"ok": False, "code": None, "message": "", "models": [], "checked": []}
    spec = PROVIDERS.get(cfg.provider)
    if spec is None:
        return {**out, "code": "not_configured", "message": "Choose a provider."}
    if spec["key"] and not cfg.api_key:
        return {**out, "code": "missing_key", "message": "Enter the provider's API key, or continue in local-only mode."}
    if spec["custom_url"] and not cfg.base_url:
        return {**out, "code": "not_configured", "message": "Enter the server's address."}
    try:
        out["models"] = llm.models()
        out["checked"].append("key")
    except LLMError as e:
        return {**out, "code": e.code, "message": e.message}
    if not cfg.model:
        return {**out, "ok": True, "message": "The key works. Choose a model."}
    if out["models"] and cfg.model not in out["models"]:
        return {**out, "code": "model_not_found", "message": f"{cfg.model} is not one of the models this key can use. Choose another."}
    try:
        llm.complete("Reply with the single word OK.", "Say OK.", max_tokens=8)
        out["checked"].append("model")
    except LLMError as e:
        return {**out, "code": e.code, "message": e.message}
    return {**out, "ok": True, "message": "The key and the model work."}
