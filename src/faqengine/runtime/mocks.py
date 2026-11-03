"""Test doubles for the provider-neutral interfaces (contracts.py). They exist so that every path of the pipeline can be tested without a network, a key or a model: a provider that answers with
whatever it is told to (a good answer, invalid JSON, a timeout), and a consent record that can be given and revoked. Anything measured with them is a measurement of the pipeline's handling, never of
a language model. Standard library only."""
from __future__ import annotations

import json

try:
    from .contracts import ProviderReply
except ImportError:  # an exported package puts runtime/ on the path
    from contracts import ProviderReply


class MockProvider:
    def __init__(self, replies=None, name: str = "mock", model: str = "mock-1"):
        self.name, self.model, self._replies = name, model, list(replies or [])
        self.calls: list[str] = []

    def complete(self, system: str, user: str, schema: dict | None = None) -> ProviderReply:
        self.calls.append(user)
        r = self._replies.pop(0) if len(self._replies) > 1 else (self._replies[0] if self._replies else ProviderReply(failure="provider:server"))
        return r(system, user) if callable(r) else r


def reply_json(verdict: str, claims: list[tuple[str, list[str]]], missing: list[str] | None = None) -> ProviderReply:
    return ProviderReply(text=json.dumps({"verdict": verdict, "missing": missing or [], "claims": [{"text": t, "evidence": ids} for t, ids in claims]}), tokens_in=100, tokens_out=40, seconds=0.01)


class MockConsent:
    """A consent record: `grant` and `revoke` take effect on the next call to `allows`."""

    def __init__(self, granted: set[tuple[str | None, str]] | None = None):
        self.granted = set(granted or ())

    def grant(self, tenant_id: str | None, provider: str) -> None:
        self.granted.add((tenant_id, provider))

    def revoke(self, tenant_id: str | None, provider: str) -> None:
        self.granted.discard((tenant_id, provider))

    def allows(self, tenant_id: str | None, provider_name: str) -> bool:
        return (tenant_id, provider_name) in self.granted
