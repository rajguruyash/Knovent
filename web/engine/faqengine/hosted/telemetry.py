"""OBSERVABILITY: structured logs with correlation ids, counters and latency windows, and alert rules over them. What may appear in any of it: counts, ids, timings, kinds of failure. What may not, and
cannot, because it is removed here before anything is written: a question, an answer, a passage, a key, a token, an address of a person. Standard library only.

    correlation ids   one per request: the caller's `X-Request-Id` if it is well formed, else a new one; it is on the response, in every log line of the request and in the stored usage rows
    log_event         one JSON line per event, fields scrubbed (names that could hold content or a secret are dropped; values that look like keys are redacted; long values cut)
    Metrics           counters, per-business counters, latency windows (median and 95th percentile), and a rolling event window for rates
    alerts            `alerts.evaluate(metrics)`: named conditions with a threshold, a window and what to do (docs/operations.md)
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import Counter, deque

CORRELATION = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
DROP = re.compile(r"(question|query|answer|content|text|passage|body|prompt|message|key|secret|token|password|authorization|bearer|cookie|credential|email)", re.I)
KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|sk-ant-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|Bearer\s+\S+)")
SAFE_NAMES = {"tokens_in", "tokens_out", "tokens", "token_count"}  # counts of tokens are counts, not tokens


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def clean_correlation(value: object) -> str | None:
    """The caller's request id if it is well formed (so that a trace can span callers), else None. Anything else is ignored: an id is never a way to put text into a log."""
    return value if isinstance(value, str) and CORRELATION.match(value) else None


def scrub(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if DROP.search(k) and k not in SAFE_NAMES:
            continue
        if isinstance(v, str):
            v = KEYLIKE.sub("[redacted]", v)[:200]
        elif isinstance(v, (dict, list, tuple, set)):
            v = KEYLIKE.sub("[redacted]", json.dumps(v, default=str))[:300]
        out[k] = v
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None)
        base = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name, "event": getattr(record, "event", record.getMessage()[:80])}
        if fields:
            base.update(scrub(fields))
        return json.dumps(base, default=str)


def configure_logging(level: str = "INFO", structured: bool = True) -> None:
    root = logging.getLogger("faqengine")
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if structured else logging.Formatter("%(asctime)s %(name)s %(message)s"))
    root.handlers[:] = [handler]


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, event, extra={"event": event, "fields": fields})


class Metrics:
    """In-process, per service instance. Cheap and bounded: counters, a few thousand latency samples per series, and ten thousand recent events for rates. The service reports it behind its secret
    (`GET /v1/metrics`); a scraper or a monitor reads it from there (docs/operations.md)."""

    def __init__(self, clock=time.time, samples: int = 5000, events: int = 10_000):
        self.clock, self._lock = clock, threading.Lock()
        self.counters: Counter = Counter()
        self.series: dict[str, deque] = {}
        self.tenants: dict[str, Counter] = {}
        self.events: deque = deque(maxlen=events)
        self.samples = samples

    def inc(self, name: str, n: int = 1, tenant: str | None = None) -> None:
        with self._lock:
            self.counters[name] += n
            self.events.append((self.clock(), name, n))
            if tenant:
                if len(self.tenants) > 5000 and tenant not in self.tenants:
                    return  # bounded: a flood of new ids does not grow it without end
                self.tenants.setdefault(tenant, Counter())[name] += n

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self.series.setdefault(name, deque(maxlen=self.samples)).append(float(value))

    @staticmethod
    def _pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        xs = sorted(values)
        pos = (len(xs) - 1) * q / 100
        lo = int(pos)
        hi = min(lo + 1, len(xs) - 1)
        return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 1)

    def latency(self, name: str) -> dict:
        with self._lock:
            v = list(self.series.get(name, ()))
        return {"n": len(v), "median_ms": self._pct(v, 50), "p95_ms": self._pct(v, 95)}

    def window(self, seconds: float) -> Counter:
        """Counts of each event in the last `seconds`."""
        cutoff = self.clock() - seconds
        out: Counter = Counter()
        with self._lock:
            for ts, name, n in reversed(self.events):
                if ts < cutoff:
                    break
                out[name] += n
        return out

    def snapshot(self, top_tenants: int = 10) -> dict:
        with self._lock:
            counters = dict(self.counters)
            busiest = sorted(self.tenants.items(), key=lambda kv: -sum(kv[1].values()))[:top_tenants]
            names = list(self.series)
        return {"counters": counters, "latency": {n: self.latency(n) for n in names}, "busiest_businesses": [{"business": k, **dict(v)} for k, v in busiest]}
