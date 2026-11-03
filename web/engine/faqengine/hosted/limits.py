"""LIMITS: what a business may use, checked on the server before a provider is called, never by the page. A rate limit per business for every mode that reaches a provider (abuse control); for
the platform's own account, a plan with EXPLICIT monthly limits (requests, tokens, estimated cost) and, for a trial, an end. A plan with a limit left empty is not "unlimited": it allows no
platform inference, because "unlimited" has no cost model. The platform's shared capacity is also protected by a cap on how many platform calls run at once.

Usage is read from the stored usage records (public.usage_records, kind llm_call), so the limits hold across instances of the service; a few concurrent requests can pass a limit together, by at
most the number of requests in flight. The month is the calendar month in UTC."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .store import Store

DEFAULT_PER_MINUTE = 20


def month_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Limits:
    def __init__(self, store: Store, default_per_minute: int = DEFAULT_PER_MINUTE, max_platform_concurrent: int = 8, clock=lambda: datetime.now(timezone.utc)):
        self.store, self.default_per_minute, self.clock = store, default_per_minute, clock
        self._slots = threading.BoundedSemaphore(max_platform_concurrent)

    def check(self, business_id: str, mode: str, tier: str | None = None, spend_cap_usd: float | None = None) -> str | None:
        """None if the business may make an inference request in this mode (and, for the platform, this tier: `free` or `paid`) now, else the reason (`limit:<kind>`). Free hosted inference has its
        own quota and never spends money; paid hosted inference has the plan's limits AND, when the business set one, its own spending cap, whichever is reached first."""
        plan = self.store.plan(business_id)
        per_minute = plan["per_minute"] if plan else self.default_per_minute
        if self.store.inference_calls_since(business_id, 60) >= per_minute:
            return "limit:rate_limited"
        if mode != "platform":
            return None  # a business's own key or machine: the provider's terms and the customer's own account apply; only the abuse limit above is ours
        now = self.clock()
        if tier == "free":
            if plan is None or plan["free_requests_per_month"] is None or plan["free_tokens_per_month"] is None:
                return "limit:no_free_quota"  # no stated free allowance is no free hosted inference
            if plan["plan"] == "trial" and plan["trial_ends_at"] is not None and plan["trial_ends_at"] <= now:
                return "limit:trial_ended"
            used = self.store.usage_period(business_id, "platform", month_start(now), "free")
            if used["requests"] >= plan["free_requests_per_month"]:
                return "limit:free_quota_requests"
            if used["tokens_in"] + used["tokens_out"] >= plan["free_tokens_per_month"]:
                return "limit:free_quota_tokens"
            return None
        if plan is None or any(plan[k] is None for k in ("requests_per_month", "tokens_per_month", "cost_cap_usd_per_month")):
            return "limit:no_plan"
        if plan["plan"] == "trial" and plan["trial_ends_at"] is not None and plan["trial_ends_at"] <= now:
            return "limit:trial_ended"
        used = self.store.usage_period(business_id, "platform", month_start(now), "paid")
        if used["requests"] >= plan["requests_per_month"]:
            return "limit:quota_requests"
        if used["tokens_in"] + used["tokens_out"] >= plan["tokens_per_month"]:
            return "limit:quota_tokens"
        if used["cost"] is not None and used["cost"] >= plan["cost_cap_usd_per_month"]:
            return "limit:quota_cost"
        if spend_cap_usd is not None and used["cost"] is not None and used["cost"] >= spend_cap_usd:
            return "limit:spend_cap"  # the business's own limit, lower than the plan's
        return None

    def take_platform_slot(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release_platform_slot(self) -> None:
        try:
            self._slots.release()
        except ValueError:  # pragma: no cover - released twice
            pass

    def summary(self, business_id: str, spend_cap_usd: float | None = None) -> dict:
        """What the page shows: the plan, this month's use per mode, and what is left of each limit (None where no limit is stated)."""
        plan = self.store.plan(business_id)
        start = month_start(self.clock())
        by_mode = {m: self.store.usage_period(business_id, m, start) for m in ("platform", "byok", "local")}
        paid = self.store.usage_period(business_id, "platform", start, "paid")
        free = self.store.usage_period(business_id, "platform", start, "free")
        left = None
        if plan and all(plan[k] is not None for k in ("requests_per_month", "tokens_per_month", "cost_cap_usd_per_month")):
            left = {"requests": max(0, plan["requests_per_month"] - paid["requests"]), "tokens": max(0, plan["tokens_per_month"] - paid["tokens_in"] - paid["tokens_out"]),
                    "cost_usd": None if paid["cost"] is None else round(max(0.0, plan["cost_cap_usd_per_month"] - paid["cost"]), 6)}
        free_left = None
        if plan and plan["free_requests_per_month"] is not None and plan["free_tokens_per_month"] is not None:
            free_left = {"requests": max(0, plan["free_requests_per_month"] - free["requests"]), "tokens": max(0, plan["free_tokens_per_month"] - free["tokens_in"] - free["tokens_out"])}
        spend_left = None if spend_cap_usd is None or paid["cost"] is None else round(max(0.0, spend_cap_usd - paid["cost"]), 6)
        return {"period_start": start.date().isoformat(), "plan": None if plan is None else {**plan, "trial_ends_at": plan["trial_ends_at"].isoformat() if plan["trial_ends_at"] else None},
                "used": by_mode, "used_free": free, "used_paid": paid, "remaining_platform": left, "platform_allowed": left is not None, "remaining_free": free_left, "free_allowed": free_left is not None,
                "spend_cap_usd": spend_cap_usd, "spend_cap_remaining_usd": spend_left}
