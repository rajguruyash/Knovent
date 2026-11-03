"""THE PROVIDER REGISTRY: which provider keys exist, which execution modes each can serve, who holds its credential, and how an adapter is built from a stored configuration. The provider key is
also the key of its disclosure (hosted/disclosure.py) and of an agreement (public.ai_inference_consents): one name end to end.

Execution modes (the same pipeline in all of them; only where inference happens and who holds the credential differ):
    platform   the platform's own account with a provider (credential from the operator's environment, never from a request)
    byok       the business's own account (credential encrypted per business, write-only)
    local      a model on the business's own machine, reached through the companion (nothing leaves it)
`third_party` says whether text sent in that mode leaves for another company: those need a verified provider and an agreement; a local one does not.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import catalogue
from .adapters import Anthropic, Gemini, OllamaNative, OpenAIStyle

MODES = ("deterministic", "platform", "byok", "local")
AI_MODES = ("platform", "byok", "local")
PROVIDERS: dict[str, dict] = {
    "cloudflare": {"label": "Cloudflare Workers AI", "family": "openai_style", "modes": ("platform",), "base_url": "https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1", "key": True, "custom_url": False, "third_party": True},
    "openrouter": {"label": "OpenRouter", "family": "openai_style", "modes": ("platform", "byok"), "base_url": "https://openrouter.ai/api/v1", "key": True, "custom_url": False, "third_party": True, "routing": True},
    "openai": {"label": "OpenAI", "family": "openai_style", "modes": ("byok",), "base_url": "https://api.openai.com/v1", "key": True, "custom_url": False, "third_party": True},
    "anthropic": {"label": "Anthropic (Claude)", "family": "anthropic", "modes": ("byok",), "base_url": "https://api.anthropic.com/v1", "key": True, "custom_url": False, "third_party": True},
    "gemini": {"label": "Google Gemini", "family": "gemini", "modes": ("byok",), "base_url": "https://generativelanguage.googleapis.com/v1beta", "key": True, "custom_url": False, "third_party": True},
    "openai_compatible": {"label": "OpenAI-compatible server", "family": "openai_style", "modes": ("byok",), "base_url": "", "key": False, "custom_url": True, "third_party": True},
    "ollama": {"label": "Ollama (on the business's own machine)", "family": "ollama", "modes": ("local",), "base_url": "http://127.0.0.1:11434", "key": False, "custom_url": False, "third_party": False},
    "llamacpp": {"label": "llama.cpp server (on the business's own machine)", "family": "openai_style", "modes": ("local",), "base_url": "http://127.0.0.1:8080/v1", "key": False, "custom_url": False, "third_party": False},
}
FAMILIES = {"openai_style": OpenAIStyle, "anthropic": Anthropic, "gemini": Gemini, "ollama": OllamaNative}


def known(provider: str) -> bool:
    return provider in PROVIDERS


def serves(provider: str, mode: str) -> bool:
    return provider in PROVIDERS and mode in PROVIDERS[provider]["modes"]


def third_party(provider: str) -> bool:
    return bool(PROVIDERS.get(provider, {}).get("third_party", True))  # an unknown provider is treated as a third party


@dataclass(frozen=True)
class PlatformCredentials:
    """The platform's own provider credentials, from the operator's environment (PLATFORM_CLOUDFLARE_ACCOUNT_ID, PLATFORM_CLOUDFLARE_API_TOKEN, PLATFORM_OPENROUTER_API_KEY). They are held here and
    handed only to an adapter; they are never in a repr, a log line, a response or a database row. Deliberately NOT the CLOUDFLARE_*/OPENROUTER_* names used by the benchmark: the benchmark's
    credentials and production's are different credentials."""

    values: tuple = ()

    @classmethod
    def from_env(cls, env=os.environ) -> PlatformCredentials:
        names = ("PLATFORM_CLOUDFLARE_ACCOUNT_ID", "PLATFORM_CLOUDFLARE_API_TOKEN", "PLATFORM_OPENROUTER_API_KEY")
        return cls(tuple((n, env[n]) for n in names if env.get(n)))

    def get(self, name: str) -> str:
        return dict(self.values).get(name, "")

    def available(self, provider: str) -> bool:
        if provider == "cloudflare":
            return bool(self.get("PLATFORM_CLOUDFLARE_ACCOUNT_ID") and self.get("PLATFORM_CLOUDFLARE_API_TOKEN"))
        if provider == "openrouter":
            return bool(self.get("PLATFORM_OPENROUTER_API_KEY"))
        return False

    def status(self) -> dict[str, str]:
        """For an operator: SET or MISSING for each name, never a value."""
        return {n: ("SET" if v else "MISSING") for n, v in {k: self.get(k) for k in ("PLATFORM_CLOUDFLARE_ACCOUNT_ID", "PLATFORM_CLOUDFLARE_API_TOKEN", "PLATFORM_OPENROUTER_API_KEY")}.items()}

    def __repr__(self) -> str:
        return f"PlatformCredentials({self.status()})"


def build(provider: str, model: str, mode: str, *, key: str = "", base_url: str = "", platform: PlatformCredentials | None = None, allow_private: bool = False, timeout: float = 60.0,
          price_in_per_m: float | None = None, price_out_per_m: float | None = None, structured: str = "auto", max_tokens: int = 1500):
    """An adapter for one provider, model and mode, from what a stored configuration says. Refuses a provider that cannot serve the mode and a mode that has no credential. Nothing here is taken from
    a request."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"unknown provider {provider!r}")
    if mode not in spec["modes"]:
        raise ValueError(f"{provider} cannot be used in {mode} mode")
    info = catalogue.lookup(provider, model)
    cred = key
    base = spec["base_url"]
    if mode == "platform":
        p = platform or PlatformCredentials()
        if not p.available(provider):
            raise ValueError(f"the platform has no credential for {provider}")
        cred = p.get("PLATFORM_CLOUDFLARE_API_TOKEN") if provider == "cloudflare" else p.get("PLATFORM_OPENROUTER_API_KEY")
        base = base.format(account=p.get("PLATFORM_CLOUDFLARE_ACCOUNT_ID")) if provider == "cloudflare" else base
    elif spec["custom_url"]:
        if not base_url:
            raise ValueError("a server address is needed")
        base = base_url
    elif mode == "byok" and spec["key"] and not key:
        raise ValueError(f"{provider} needs the business's API key")
    elif mode == "local" and base_url:
        base = base_url  # the companion's runtime address (loopback), from the companion's own configuration
    kw = dict(name=provider, model=model, mode=mode, base_url=base, key=cred, timeout=timeout, allow_private=allow_private or mode == "local", structured=(info.structured if info and structured == "auto" else structured),
              extra_body=(info.extra_body if info else {}), price_in_per_m=price_in_per_m if price_in_per_m is not None else (info.price_in_per_m if info else None),
              price_out_per_m=price_out_per_m if price_out_per_m is not None else (info.price_out_per_m if info else None), max_tokens=max_tokens)
    if kw["structured"] == "unverified":
        kw["structured"] = "prompt"  # a model whose support is not verified is asked for JSON in words, and its answer validated
    cls = FAMILIES[spec["family"]]
    return cls(require_structured_routing=bool(spec.get("routing")), **kw) if cls is OpenAIStyle else cls(**kw)
