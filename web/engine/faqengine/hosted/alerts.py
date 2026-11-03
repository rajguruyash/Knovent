"""ALERT RULES over the service's metrics (telemetry.Metrics): named conditions with a window, a threshold and a severity. `evaluate` is pure: it reads counters and returns what is firing now, so
the same function serves `GET /v1/alerts`, a scheduled check, and the tests. Delivery (a page, a message) is the monitor's job: it polls `/v1/alerts` and acts on `firing` (docs/operations.md,
which says what to do for each). Thresholds are starting points chosen without production traffic; change them where they are wrong."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

WINDOW = 300.0  # seconds


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str  # critical | warning | info
    says: str
    check: object  # (window Counter, latency dict, breaker states) -> (firing, detail)


def _rate(num: int, den: int, min_den: int, limit: float):
    return den >= min_den and num / den > limit, f"{num} of {den}"


RULES = (
    Rule("answers_failing", "critical", "more than 5% of requests ended in a server error", lambda w, lat, b: _rate(w["request_error"], w["request"], 20, 0.05)),
    Rule("pipeline_faults", "critical", "the reasoning pipeline raised errors (the existing path answered instead)", lambda w, lat, b: (w["pipeline_error"] >= 3, f"{w['pipeline_error']} in the window")),
    Rule("provider_failing", "warning", "more than 20% of provider calls failed", lambda w, lat, b: _rate(w["provider_failure"], w["provider_call"], 10, 0.20)),
    Rule("provider_down", "critical", "more than half of provider calls failed", lambda w, lat, b: _rate(w["provider_failure"], w["provider_call"], 10, 0.50)),
    Rule("circuit_open", "warning", "a provider's circuit breaker is open: calls to it fail at once", lambda w, lat, b: (any(s == "open" for s in b.values()), ", ".join(k for k, s in b.items() if s == "open"))),
    Rule("latency_p95_high", "warning", "the 95th percentile of whole-request time is above 15 seconds", lambda w, lat, b: ((lat.get("request_ms", {}).get("n", 0) >= 20 and (lat.get("request_ms", {}).get("p95_ms") or 0) > 15000), str(lat.get("request_ms", {}).get("p95_ms")))),
    Rule("claims_rejected_often", "warning", "more than 20% of model answers lost a claim to verification", lambda w, lat, b: _rate(w["claims_rejected"], w["model_answer"], 20, 0.20)),
    Rule("consent_refusals", "warning", "many AI requests were refused for want of consent or verified terms (a misconfiguration, or a consent withdrawn while a client keeps asking)", lambda w, lat, b: (w["consent_refused"] >= 20, f"{w['consent_refused']} in the window")),
    Rule("quota_refusals", "info", "a business hit its plan limit", lambda w, lat, b: (w["quota_refused"] >= 1, f"{w['quota_refused']} in the window")),
    Rule("rate_limit_events", "info", "requests were slowed by a rate limit", lambda w, lat, b: (w["rate_limited"] >= 10, f"{w['rate_limited']} in the window")),
    Rule("security_events", "warning", "repeated refused attempts: wrong role, another business, a bad service secret", lambda w, lat, b: (w["security_denied"] >= 5, f"{w['security_denied']} in the window")),
)


def evaluate(metrics, breaker_states: dict[str, str] | None = None, window: float = WINDOW) -> list[dict]:
    w: Counter = metrics.window(window)
    lat = {n: metrics.latency(n) for n in list(metrics.series)}
    out = []
    for r in RULES:
        firing, detail = r.check(w, lat, breaker_states or {})
        out.append({"name": r.name, "severity": r.severity, "says": r.says, "firing": bool(firing), "detail": detail if firing else ""})
    return out


def firing(metrics, breaker_states=None, window: float = WINDOW) -> list[dict]:
    return [a for a in evaluate(metrics, breaker_states, window) if a["firing"]]
