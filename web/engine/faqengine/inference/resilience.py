"""TIMEOUTS, BOUNDED RETRIES AND A CIRCUIT BREAKER around one adapter. Three rules:

  * Retries are of the SAME provider and model and are bounded (attempts, total deadline). Only failures worth retrying are retried (errors.py); a bad key, an unknown model, a blocked address or
    a quota is not. There is no failover: when an adapter cannot answer, the answer is a failure that says so, and what happens next (the deterministic answer, or "unavailable") is the pipeline's
    decision under the business's own setting, never a quiet call to a different provider.
  * A circuit breaker per (mode, provider, model) opens after consecutive failures that are the PROVIDER's health (rate limits, outages, timeouts, network), not the caller's mistakes. While open,
    calls fail at once with `provider:circuit_open` instead of waiting for another timeout; after a cool-down one probe is let through, and its result closes or re-opens it.
  * Cancellation is cooperative: checked before every attempt and between retries. (A request already in flight to a cloud provider ends at its timeout; the local companion can really stop one.)

The clock and the sleep are injectable so that all of this is tested without waiting. Standard library only.
"""
from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import Any

from ..runtime.contracts import ProviderReply
from .errors import COUNTS_AGAINST_A_PROVIDER


class CircuitBreaker:
    def __init__(self, failures: int = 5, cooldown: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown, self.clock = failures, cooldown, clock
        self._lock = threading.Lock()
        self._fails = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            return "half_open" if self.clock() - self._opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        """May a call be made now? Closed: yes. Open: no. After the cool-down: exactly one probe at a time."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self.clock() - self._opened_at >= self.cooldown and not self._probing:
                self._probing = True
                return True
            return False

    def record(self, failure: str | None) -> None:
        """The outcome of an allowed call: None for success, else the failure ('provider:<kind>')."""
        kind = (failure or "").removeprefix("provider:")
        with self._lock:
            self._probing = False
            if failure is None:
                self._fails, self._opened_at = 0, None
            elif kind in COUNTS_AGAINST_A_PROVIDER:
                self._fails += 1
                if self._fails >= self.threshold or self._opened_at is not None:
                    self._opened_at = self.clock()
            # any other failure (a bad key, an unknown model, a cancelled call) says nothing about the provider's health: the count is left as it was


class Breakers:
    """One breaker per (mode, provider, model[, business for a customer's own key: one customer's bad key must not close the circuit for another])."""

    def __init__(self, **kw):
        self.kw, self._by, self._lock = kw, {}, threading.Lock()

    def get(self, key: tuple) -> CircuitBreaker:
        with self._lock:
            return self._by.setdefault(key, CircuitBreaker(**self.kw))

    def states(self) -> dict[str, str]:
        """mode/provider/model -> the worst state among its breakers (a business's own key has its own breaker, and one business's open circuit shows here as open): no business is named."""
        rank = {"closed": 0, "half_open": 1, "open": 2}
        with self._lock:
            out: dict[str, str] = {}
            for k, b in self._by.items():
                label, state = "/".join(str(x) for x in k[:3]), b.state
                if rank[state] >= rank[out.get(label, "closed")]:
                    out[label] = state
            return out


class Resilient:
    """A Provider (contracts.Provider) made of an adapter, a breaker and a retry policy. `complete` never raises."""

    def __init__(self, adapter, breaker: CircuitBreaker, attempts: int = 2, backoff: float = 0.5, deadline: float = 90.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, jitter: Callable[[], float] = random.random, cancel: Any = None):
        self.adapter, self.breaker, self.attempts, self.backoff, self.deadline = adapter, breaker, max(1, attempts), backoff, deadline
        self.sleep, self.clock, self.jitter, self.cancel = sleep, clock, jitter, cancel
        self.name, self.model, self.mode = adapter.name, adapter.model, adapter.mode

    def complete(self, system: str, user: str, schema: dict | None = None) -> ProviderReply:
        started = self.clock()
        total = 0
        last = ProviderReply(failure="provider:not_connected")
        for n in range(1, self.attempts + 1):
            if self.cancel is not None and self.cancel.is_set():
                return ProviderReply(failure="provider:cancelled", attempts=total, model=self.model, seconds=self.clock() - started)
            if not self.breaker.allow():
                return ProviderReply(failure="provider:circuit_open", attempts=total, model=self.model, seconds=self.clock() - started)
            last = self.adapter.complete(system, user, schema, self.cancel)
            total += last.attempts
            self.breaker.record(last.failure)
            if last.failure is None:
                last.attempts, last.seconds = total, self.clock() - started
                return last
            kind = last.failure.removeprefix("provider:")
            if kind not in COUNTS_AGAINST_A_PROVIDER or n == self.attempts:
                break
            wait = min(self.backoff * (2 ** (n - 1)) * (0.5 + self.jitter()), max(0.0, self.deadline - (self.clock() - started)))
            if self.clock() - started + wait >= self.deadline:
                break  # no time left for another try
            self.sleep(wait)
        last.attempts, last.seconds = total, self.clock() - started
        return last

    def health(self) -> dict:
        h = self.adapter.health()
        return {**h, "circuit": self.breaker.state}
