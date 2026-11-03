"""Every way a provider can fail, named once. A failure of a PROVIDER (it did not answer: auth, rate limit, outage, timeout, network, quota, a refusal to accept the request) is kept apart from a
failure of the REASONING (it answered and the answer was not usable: invalid JSON, a claim the evidence does not carry): the first is `provider:<kind>`, the second `reasoning:<kind>`
(runtime/compose.py, runtime/pipeline.py). Nothing here carries a key or any text of a request."""
from __future__ import annotations

from dataclasses import dataclass

try:
    from ..runtime.llm import LLMError
except ImportError:  # pragma: no cover
    LLMError = Exception  # type: ignore[misc,assignment]

# LLMError.code (runtime/llm.py) -> (failure kind, worth retrying against the SAME provider and model)
CLASSIFY = {
    "invalid_key": ("auth", False), "forbidden": ("auth", False), "missing_key": ("auth", False),
    "model_not_found": ("not_found", False), "not_configured": ("not_configured", False),
    "rate_limited": ("rate_limited", True), "quota": ("quota", False),
    "unavailable": ("server", True), "bad_response": ("server", True),
    "timeout": ("timeout", True), "network": ("network", True),
    "bad_request": ("bad_request", False), "blocked_endpoint": ("blocked", False),
}
KINDS = ("auth", "not_found", "not_configured", "rate_limited", "quota", "server", "timeout", "network", "bad_request", "blocked", "cancelled", "circuit_open", "not_connected")
COUNTS_AGAINST_A_PROVIDER = {"rate_limited", "server", "timeout", "network"}  # what opens a circuit breaker: the provider's health, not the caller's mistakes (a bad key is not an outage)


@dataclass(frozen=True)
class Classified:
    kind: str
    retryable: bool

    @property
    def failure(self) -> str:
        return f"provider:{self.kind}"


def classify(error: BaseException) -> Classified:
    code = getattr(error, "code", None)
    kind, retry = CLASSIFY.get(str(code), ("server", False))
    return Classified(kind, retry)
