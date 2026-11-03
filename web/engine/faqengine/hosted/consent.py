"""The server-side consent check, and the stand-ins the pipeline needs to say why a route was refused. Nothing here sends anything."""
from __future__ import annotations

from ..runtime.contracts import ProviderReply
from . import disclosure
from .store import Store


class DbConsent:
    """The server-side consent check (contracts.ConsentPolicy), answered by the database on EVERY call, never cached: a revoked agreement stops the next external call. It is for one business: a
    request for another is refused. Consent is for one provider and one disclosure version; a provider with no disclosure text, or whose terms have not been verified, cannot be used."""

    def __init__(self, store: Store, business_id: str):
        self.store, self.business_id = store, business_id

    def allows(self, tenant_id: str | None, provider_name: str) -> bool:
        if tenant_id != self.business_id or not disclosure.verified(provider_name):  # a provider whose terms are not verified is not used, whatever an earlier agreement says
            return False
        return self.store.consent_allows(self.business_id, provider_name, disclosure.current_version(provider_name))


class Unconsented:
    """A provider the business has configured and not agreed to send data to. It has a name, so the consent check can say no to it; it cannot be called."""

    def __init__(self, name: str, model: str = ""):
        self.name, self.model = name, model

    def complete(self, system: str, user: str, schema: dict | None = None) -> ProviderReply:  # pragma: no cover - the consent check stops before this
        return ProviderReply(failure="provider:not_connected")


class Refused:
    """A route the router could not give: it carries the reason (`refusal`, e.g. `limit:quota_requests`, `provider:circuit_open`, `local:no_companion`) so that the pipeline says it as it was and
    sends nothing. It has no way to call anything."""

    def __init__(self, name: str, refusal: str, model: str = ""):
        self.name, self.model, self.refusal = name, model, refusal

    def complete(self, system: str, user: str, schema: dict | None = None) -> ProviderReply:  # pragma: no cover
        return ProviderReply(failure="provider:not_connected")


class ClientSide:
    """Names the model that will be run on the business's own machine, so that the pipeline can label the result. It has no way to call anything: the call is made by the person's browser to their companion."""

    def __init__(self, name: str, model: str = ""):
        self.name, self.model = name, model

    def complete(self, system: str, user: str, schema: dict | None = None) -> ProviderReply:  # pragma: no cover
        return ProviderReply(failure="provider:not_connected")
