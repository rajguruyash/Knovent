"""PROVIDER ADAPTERS: one contract (below), one adapter per API family. Each knows how its provider wants a request written and how its answer is read, and nothing else: no business, no document,
no consent, no quota (the router decides whether a call may be made; the adapter only makes it).

    complete(system, user, schema=None, cancel=None) -> ProviderReply     never raises: a failure is `reply.failure == "provider:<kind>"` (errors.py), tokens, latency and an estimated cost come back
    models() -> list[str]                                                 discovery with the credential, or the configured model checked against what the provider lists
    health() -> {ok, latency_ms, kind, detail}                            one cheap call; never any text of a request
    estimate_cost(tokens_in, tokens_out) -> float | None                  from a price somebody configured or verified; None means unknown, never zero

Families: OpenAIStyle (OpenAI, OpenRouter, Cloudflare Workers AI, any OpenAI-compatible server, llama.cpp's server), Anthropic, Gemini, OllamaNative.

STRUCTURED OUTPUT. Whether a given MODEL supports a JSON schema is a fact about that model, not about its provider, so it is never assumed. `structured` is "json_schema" / "json_object" only where the
catalogue (catalogue.py) says so for that model, or "auto": the adapter tries the provider family's documented mechanism, and if the provider refuses the parameter (HTTP 400 naming it) it asks the SAME
model again in prompt-only mode (JSON requested in the words of the prompt, validated by runtime/compose.py either way). That is parameter negotiation with one model, never a switch to another;
the extra request is counted in `attempts`. Anything a model returns is untrusted until the pipeline has verified it.

NETWORK. Every request goes through runtime/llm.request: no redirects are followed, responses are size-capped, and for a hosted deployment (`allow_private=False`) the address actually connected to
must be public (SSRF). Keys are sent only in a header to the provider's host and are redacted from every message.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

from ..runtime.contracts import ProviderReply
from ..runtime.llm import LLMError, _error_for, redact, request
from .errors import classify

STRUCTURED_REFUSAL_WORDS = ("response_format", "json_schema", "schema", "tool", "responsemime", "responseschema", "format", "structured")


class Adapter:
    """The contract and the parts every family shares. Subclasses write `_request` (how this API wants it) and `_read` (how its answer is read)."""

    family = "base"
    default_structured = "unverified"  # what 'auto' tries first, from the family's documented mechanism; refused parameters fall back to prompt-only

    def __init__(self, *, name: str, model: str, mode: str, base_url: str, key: str = "", timeout: float = 60.0, allow_private: bool = False, structured: str = "auto", extra_body: dict | None = None,
                 extra_headers: dict | None = None, price_in_per_m: float | None = None, price_out_per_m: float | None = None, max_tokens: int = 1500):
        self.name, self.model, self.mode = name, model, mode
        self.base_url, self._key, self.timeout, self.allow_private = base_url.rstrip("/"), key, timeout, allow_private
        self.structured, self.extra_body, self.extra_headers, self.max_tokens = structured, dict(extra_body or {}), dict(extra_headers or {}), max_tokens
        self.price_in_per_m, self.price_out_per_m = price_in_per_m, price_out_per_m

    def __repr__(self) -> str:  # never the key
        return f"<{type(self).__name__} {self.name}:{self.model} mode={self.mode}>"

    # ---- family-specific
    def _headers(self) -> dict:
        return {**({"Authorization": f"Bearer {self._key}"} if self._key else {}), **self.extra_headers}

    def _request(self, system: str, user: str, schema: dict | None, structured: str, negotiated: set[str]) -> tuple[str, dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def _read(self, data: dict, schema: dict | None) -> tuple[str, int, int]:  # pragma: no cover - interface
        raise NotImplementedError

    def _list_models(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    # ---- contract
    def estimate_cost(self, tokens_in: int, tokens_out: int) -> float | None:
        if self.price_in_per_m is None or self.price_out_per_m is None:
            return None
        return tokens_in / 1e6 * self.price_in_per_m + tokens_out / 1e6 * self.price_out_per_m

    def models(self) -> list[str]:
        try:
            return self._list_models()
        except LLMError as e:
            raise LLMError(e.code, redact(e.message, self._key)) from None

    def health(self) -> dict:
        started = time.perf_counter()
        try:
            listed = self.models()
        except LLMError as e:
            c = classify(e)
            return {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "kind": c.kind, "detail": redact(e.message, self._key)[:160]}
        found = not listed or self.model in listed
        return {"ok": found, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "kind": None if found else "not_found", "detail": "" if found else "the configured model is not one the provider lists"}

    def complete(self, system: str, user: str, schema: dict | None = None, cancel: Any = None) -> ProviderReply:
        started = time.perf_counter()
        if cancel is not None and cancel.is_set():
            return ProviderReply(failure="provider:cancelled", model=self.model, attempts=0)
        structured = self._structured(schema)
        negotiated: set[str] = set()
        attempts = 0
        try:
            while True:
                attempts += 1
                path, body = self._request(system, user, schema, structured, negotiated)
                status, data = request("POST", f"{self.base_url}{path}", {**self._headers(), **self._extra_for(path)}, body, self.timeout, self.allow_private)
                if status == 400 and attempts < 4:
                    why = self._negotiate(data, schema, structured, negotiated)
                    if why == "structured":
                        structured = "prompt"
                        continue
                    if why:  # a same-model parameter the provider names as unsupported (temperature, max_tokens): asked again without it
                        continue
                if status != 200:
                    raise _error_for(status, data, self._key)
                text, tin, tout = self._read(data, schema)
                if cancel is not None and cancel.is_set():
                    return ProviderReply(failure="provider:cancelled", model=self.model, attempts=attempts, seconds=time.perf_counter() - started)
                return ProviderReply(text=text, tokens_in=tin, tokens_out=tout, seconds=time.perf_counter() - started, cost=self.estimate_cost(tin, tout), attempts=attempts, model=self.model)
        except LLMError as e:
            return ProviderReply(failure=classify(e).failure, seconds=time.perf_counter() - started, attempts=attempts, model=self.model)

    # ---- shared helpers
    def _extra_for(self, path: str) -> dict:
        return {}

    def _structured(self, schema: dict | None) -> str:
        if not schema:
            return "prompt"
        return self.default_structured if self.structured == "auto" else self.structured

    def _negotiate(self, data: dict, schema: dict | None, structured: str, negotiated: set[str]) -> str | None:
        """A 400 that names a parameter this MODEL does not take: which one to drop or rename for the next request to the same model (None: not a negotiation, a real refusal)."""
        text = json.dumps(data).lower()
        if "temperature" in text and "temperature" not in negotiated:
            negotiated.add("temperature")
            return "temperature"
        if "max_completion_tokens" in text and "max_completion_tokens" not in negotiated:
            negotiated.add("max_completion_tokens")
            return "max_completion_tokens"
        if schema and structured != "prompt" and any(w in text for w in STRUCTURED_REFUSAL_WORDS):
            return "structured"
        return None


class OpenAIStyle(Adapter):
    """OpenAI, OpenRouter, Cloudflare Workers AI (its OpenAI-compatible route), any OpenAI-compatible server, llama.cpp's server."""

    family = "openai_style"
    default_structured = "json_schema"

    def __init__(self, *, require_structured_routing: bool = False, **kw):
        super().__init__(**kw)
        self.require_structured_routing = require_structured_routing  # OpenRouter: only route to an endpoint that supports the parameters sent

    def _request(self, system, user, schema, structured, negotiated):
        body: dict = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], **self.extra_body}
        if "temperature" not in negotiated:
            body["temperature"] = 0
        body["max_completion_tokens" if "max_completion_tokens" in negotiated else "max_tokens"] = self.max_tokens
        if schema and structured == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        elif schema and structured == "json_object":
            body["response_format"] = {"type": "json_object"}
        if schema and structured in ("json_schema", "json_object") and self.require_structured_routing:
            body["provider"] = {**(body.get("provider") or {}), "require_parameters": True}
        return "/chat/completions", body

    def _read(self, data, schema):
        try:
            u = data.get("usage") or {}
            return data["choices"][0]["message"]["content"] or "", int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("bad_response", "the provider's answer was not in the expected form") from None

    def _list_models(self):
        status, data = request("GET", f"{self.base_url}/models", self._headers(), None, self.timeout, self.allow_private)
        if status != 200:
            raise _error_for(status, data, self._key)
        return sorted({m.get("id", "") for m in data.get("data", []) if m.get("id")})


class Anthropic(Adapter):
    family = "anthropic"
    default_structured = "json_schema"  # realised as a forced tool call, Anthropic's documented way to get a structured result

    def _headers(self):
        return {"x-api-key": self._key, "anthropic-version": "2023-06-01", **self.extra_headers}

    def _request(self, system, user, schema, structured, negotiated):
        body: dict = {"model": self.model, "max_tokens": self.max_tokens, "system": system, "messages": [{"role": "user", "content": user}], **self.extra_body}
        if "temperature" not in negotiated:
            body["temperature"] = 0
        if schema and structured in ("json_schema", "json_object"):
            body["tools"] = [{"name": "answer", "description": "Return the answer as structured data.", "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": "answer"}
        return "/messages", body

    def _read(self, data, schema):
        try:
            u = data.get("usage") or {}
            blocks = data["content"]
            for b in blocks:
                if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                    return json.dumps(b["input"]), int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text"), int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
        except (KeyError, TypeError, AttributeError):
            raise LLMError("bad_response", "the provider's answer was not in the expected form") from None

    def _list_models(self):
        status, data = request("GET", f"{self.base_url}/models?limit=100", self._headers(), None, self.timeout, self.allow_private)
        if status != 200:
            raise _error_for(status, data, self._key)
        return sorted({m.get("id", "") for m in data.get("data", []) if m.get("id")})


class Gemini(Adapter):
    family = "gemini"
    default_structured = "json_schema"  # responseMimeType + responseSchema

    def _headers(self):
        return {"x-goog-api-key": self._key, **self.extra_headers}  # in a header, not the address: addresses end up in logs

    def _request(self, system, user, schema, structured, negotiated):
        cfg: dict = {"maxOutputTokens": self.max_tokens, **self.extra_body.get("generationConfig", {})}
        if "temperature" not in negotiated:
            cfg["temperature"] = 0
        if schema and structured in ("json_schema", "json_object"):
            cfg["responseMimeType"] = "application/json"
            if structured == "json_schema":
                cfg["responseSchema"] = schema
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": cfg}
        return f"/models/{urllib.parse.quote(self.model, safe='')}:generateContent", body

    def _read(self, data, schema):
        try:
            u = data.get("usageMetadata") or {}
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]), int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("bad_response", "the provider's answer was not in the expected form") from None

    def _list_models(self):
        status, data = request("GET", f"{self.base_url}/models?pageSize=200", self._headers(), None, self.timeout, self.allow_private)
        if status != 200:
            raise _error_for(status, data, self._key)
        return sorted({m["name"].split("/", 1)[-1] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])})


class OllamaNative(Adapter):
    """Ollama's own /api/chat (a local runtime: the companion's, never a customer-supplied address in the hosted service)."""

    family = "ollama"
    default_structured = "json_schema"

    def _request(self, system, user, schema, structured, negotiated):
        body: dict = {"model": self.model, "stream": False, "think": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                      "options": {"temperature": 0, "num_predict": self.max_tokens, **self.extra_body.get("options", {})}}
        if schema and structured in ("json_schema", "json_object"):
            body["format"] = schema if structured == "json_schema" else "json"
        return "/api/chat", body

    def _read(self, data, schema):
        try:
            return (data.get("message") or {})["content"], int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
        except (KeyError, TypeError):
            raise LLMError("bad_response", "the runtime's answer was not in the expected form") from None

    def _list_models(self):
        status, data = request("GET", f"{self.base_url}/api/tags", self._headers(), None, self.timeout, self.allow_private)
        if status != 200:
            raise _error_for(status, data, self._key)
        return sorted({m.get("name", "") for m in data.get("models", []) if m.get("name")})
