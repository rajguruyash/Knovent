"""THE ROUTER: for one business, on the server, which execution mode, provider, model and credential this request may use, or why none. It reads only what the database holds (the business's
settings, its stored configuration, its consent, its plan and usage) and what the operator configured: nothing it decides comes from the request, and a provider or model named by a browser is
never an input.

LOCAL FIRST. The business's settings name a first step (`mode`: its own computer, its own provider account, or deterministic) and, optionally, the steps that may follow it, always in this order:
its own provider account (BYOK) → FREE hosted inference → PAID hosted inference. Every follower is off unless the business turned it on, and each one is judged on its own: it is used only if the
operator switched its mode on, its provider's terms are VERIFIED, the business has agreed to THAT provider's text, the limits allow it, and its circuit is closed. `plan` returns every step with its
verdict; the service walks them in order and stops at the first that answers.

Per step: the operator's switch for the mode (a kill switch) → the step's own configuration → for a third-party provider, its disclosure, its VERIFIED terms and the business's CONSENT → limits
(rate, plan, trial, free quota, paid quota and the business's own spending cap, shared capacity) → the circuit breaker. A refused step is skipped, never "fixed": in particular:

  * a step is only ever followed by a step the business enabled. Nothing is added by the service, the browser or a failure; a provider for which there is no consent is never reached, whatever
    came before it. If no step is permitted and usable, what happens is the deterministic answer if the business allows it (`fallback`), or "unavailable";
  * the credential is decrypted only AFTER every check above has passed, and only for the adapter that makes the call;
  * `local` is decided here but carried out by the person's own browser and companion (the evidence goes to their machine, and to no provider).

`route` makes the decision and builds nothing; `provider` builds the adapter for an accepted route. Standard library only, plus the hosted store.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..inference import catalogue, registry
from ..inference.resilience import Breakers, Resilient
from ..runtime.llm import LLMError
from . import disclosure
from .consent import DbConsent
from .limits import Limits
from .secrets import SecretBox, SecretError
from .store import Store

log = logging.getLogger("faqengine.hosted.router")


@dataclass(frozen=True)
class Route:
    mode: str  # deterministic | platform | byok | local
    provider: str | None = None
    model: str | None = None
    fallback: str = "deterministic"  # what to do if the chosen AI cannot answer: the deterministic answer, or "none" (say it is unavailable)
    refusal: str | None = None  # why the chosen mode cannot be used now: consent:*, limit:*, config:*, provider:circuit_open, local:*
    client_side: bool = False  # local: inference is made by the person's own browser and companion
    companion: dict | None = None
    tier: str | None = None  # which step this is: local | byok | free | paid

    @property
    def ok(self) -> bool:
        return self.refusal is None


@dataclass(frozen=True)
class Plan:
    """The business's steps, in the order they would be tried, each with its verdict now (`Route.refusal` says why a step cannot be used). Empty for a deterministic-only business."""
    steps: tuple[Route, ...] = ()
    fallback: str = "deterministic"

    @property
    def first_usable(self) -> Route | None:
        return next((r for r in self.steps if r.ok), None)


def tier_of(info) -> str:
    """The hosted tier of a catalogue entry: the trial and best-effort free allowances are `free` (they spend nothing and have a quota); anything else is `paid`."""
    return "free" if info.tier in ("free_trial", "best_effort_free") else "paid"


class Router:
    def __init__(self, store: Store, secrets: SecretBox | None, platform: registry.PlatformCredentials, breakers: Breakers, limits: Limits, enabled_modes: frozenset[str] = frozenset(),
                 allow_private: bool = False, timeout: float = 60.0, attempts: int = 2):
        self.store, self.secrets, self.platform, self.breakers, self.limits = store, secrets, platform, breakers, limits
        self.enabled_modes, self.allow_private, self.timeout, self.attempts = enabled_modes, allow_private, timeout, attempts

    def plan(self, business_id: str) -> Plan:
        s = self.store.ai_settings(business_id)
        mode, fallback = s["mode"], s["fallback"]
        if mode == "deterministic":
            return Plan((), fallback)
        order = [mode]
        for flag, step in (("fallback_byok", "byok"), ("fallback_free", "free"), ("fallback_paid", "paid")):
            if s.get(flag) and step != mode:
                order.append(step)
        return Plan(tuple(self._step(business_id, s, step, fallback) for step in order), fallback)

    def route(self, business_id: str) -> Route:
        """The FIRST step (what a business with no followers has): kept for callers that only ask what the business's chosen mode does."""
        p = self.plan(business_id)
        return p.steps[0] if p.steps else Route("deterministic", fallback=p.fallback)

    def hosted_model(self, tier: str):
        """The model the platform uses for a hosted tier: the first one the operator released for that tier, whose provider the platform holds a credential for. (A business does not pick a model: it
        agrees to the provider, and the screen says which model that is.) Returns (info, refusal)."""
        offered = [m for m in catalogue.platform_models() if tier_of(m) == tier and registry.serves(m.provider, "platform")]
        if not offered:
            return None, "config:no_hosted_model"
        usable = [m for m in offered if self.platform.available(m.provider)]
        return (usable[0], None) if usable else (offered[0], "config:platform_unavailable")

    def _step(self, business_id: str, s: dict, step: str, fallback: str) -> Route:
        mode = {"free": "platform", "paid": "platform"}.get(step, step)

        def refuse(why: str, provider: str | None = None, model: str | None = None, tier: str | None = None, **kw) -> Route:
            return Route(mode, provider, model, fallback, refusal=why, tier=tier or step if step in ("local", "byok", "free", "paid") else tier, **kw)

        if mode not in self.enabled_modes:
            return refuse("config:ai_disabled")  # the operator has not switched this mode on
        if step == "local":
            comp = self.store.active_companion(business_id)
            if not comp:
                return refuse("local:no_companion")
            if comp.get("fit") == "not_advised":
                return refuse("local:unsuitable_hardware", comp["runtime"], comp["model"], companion=comp)  # the companion itself said this computer cannot run this model comfortably
            return Route("local", comp["runtime"], comp["model"], fallback, client_side=True, companion=comp, tier="local")  # no third party: no consent applies; nothing leaves the person's machine
        tier: str | None
        if step == "byok":
            cfg = self.store.llm_config(business_id)
            if not cfg or not cfg["enabled"] or not cfg["model"] or not registry.serves(cfg["provider"], "byok"):
                return refuse("config:byok_not_configured")
            provider, model, tier = cfg["provider"], cfg["model"], "byok"
            if registry.PROVIDERS[provider]["key"] and not cfg["has_key"]:
                return refuse("config:byok_key_missing", provider, model)
        elif step in ("free", "paid"):
            info, why = self.hosted_model(step)
            if info is None:
                return refuse(why or "config:no_hosted_model")
            provider, model, tier = info.provider, info.id, step
            if why:
                return refuse(why, provider, model)
        elif step == "platform":  # a business whose FIRST step is a platform model it was given (older settings): the model comes from its settings
            provider, model = s["platform_provider"], s["platform_model"]
            info = catalogue.lookup(provider or "", model or "")
            if info is None or not info.platform_offered or not registry.serves(provider or "", "platform"):
                return refuse("config:model_not_offered", provider, model)
            tier = tier_of(info)
            if not self.platform.available(provider or ""):
                return refuse("config:platform_unavailable", provider, model, tier)
        else:  # pragma: no cover - the table's check forbids it
            return refuse("config:unknown_mode")
        # a third party will receive the text: its terms must be verified and the business must have agreed to THIS text
        if not disclosure.known(provider):
            return refuse("consent:no_disclosure", provider, model, tier)
        if not disclosure.verified(provider):
            return refuse("consent:provider_unverified", provider, model, tier)
        if not DbConsent(self.store, business_id).allows(business_id, provider):
            return refuse("consent:missing_or_revoked", provider, model, tier)
        why = self.limits.check(business_id, mode, tier if mode == "platform" else None, s.get("paid_cap_usd") if tier == "paid" else None)
        if why:
            return refuse(why, provider, model, tier)
        if self.breakers.get(self._breaker_key(mode, provider, model, business_id)).state == "open":
            return refuse("provider:circuit_open", provider, model, tier)
        return Route(mode, provider, model, fallback, tier=tier)

    @staticmethod
    def _breaker_key(mode: str, provider: str, model: str, business_id: str) -> tuple:
        return (mode, provider, model, business_id) if mode == "byok" else (mode, provider, model)  # one customer's own key has its own circuit

    def provider(self, business_id: str, route: Route, cancel=None):
        """The provider for an accepted route: an adapter inside timeouts, bounded retries and its circuit breaker. The only place a stored key is decrypted. Raises `RouteError` (never a
        message with a key in it) when the stored configuration cannot be used."""
        if not route.ok or route.client_side or route.mode not in registry.AI_MODES:
            raise RouteError("config:not_callable_here")
        try:
            if route.mode == "platform":
                adapter = registry.build(route.provider, route.model, "platform", platform=self.platform, timeout=self.timeout, allow_private=self.allow_private)  # (private addresses only where the operator allowed them: tests)
            else:
                cfg = self.store.llm_config(business_id) or {}
                key = ""
                secret = self.store.llm_secret(business_id)
                if secret is not None:
                    if self.secrets is None:
                        raise RouteError("config:byok_key_unusable")
                    try:
                        key = self.secrets.decrypt(secret[0], business_id, secret[1])
                    except SecretError:
                        log.warning("a stored credential could not be decrypted for business %s", business_id)
                        raise RouteError("config:byok_key_unusable") from None
                adapter = registry.build(route.provider, route.model, "byok", key=key, base_url=cfg.get("base_url") or "", allow_private=self.allow_private, timeout=self.timeout)
        except (ValueError, LLMError):
            raise RouteError("config:byok_not_configured" if route.mode == "byok" else "config:platform_unavailable") from None
        return Resilient(adapter, self.breakers.get(self._breaker_key(route.mode, route.provider, route.model, business_id)), attempts=self.attempts, cancel=cancel)


class RouteError(Exception):
    """The route was accepted but its adapter cannot be built. `reason` is the refusal to report."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
