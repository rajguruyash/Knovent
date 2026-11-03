"""The hosted compute service. Vercel's functions authenticate the person and check their role, then call this service with a shared secret; this service checks the person's role AGAIN in
the database before touching a business's knowledge, so a mistake in the web layer cannot open another business's data.

    HostedEngine   all the behaviour, as plain methods (tested without HTTP)
    serve()        a small HTTP server around it (standard library), for a container or any always-on host

It loads the embedding model once, keeps each business's index in memory (rebuilt when the business's knowledge changes) and uses the same retrieval, evidence, verification and response
code as the local engine (runtime/answer.py). Language-model keys are decrypted only for the length of a request.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict, deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from .. import web as webpage
from ..kb import load_kb
from ..runtime import answer, evidence, safety
from ..runtime import llm as L
from ..inference import catalogue, registry
from ..inference.resilience import Breakers
from ..runtime import compose, shadow
from ..runtime.contracts import ProviderReply
from ..runtime.embedder import Embedder
from ..runtime.pipeline import Pipeline
from ..runtime.retrieval import CANDIDATES, DEFAULT_ALPHA, HybridIndex
from ..runtime.textutil import normalize_query
from ..runtime.validity import inactive_reasons, parse_date
from ..sources import slug
from . import alerts, disclosure, telemetry
from .scanner import ClamdScanner, Infected, NullScanner, ScanError
from .consent import ClientSide, DbConsent, Refused, Unconsented  # noqa: F401 - re-exported: tests and callers use them from here
from .limits import Limits
from .router import Plan, Route, Router, RouteError
from .secrets import SecretBox, SecretError
from .store import Store, is_uuid, text_hash

log = logging.getLogger("faqengine.hosted")
READ, WRITE, ADMIN = ("owner", "admin", "editor", "viewer"), ("owner", "admin", "editor"), ("owner", "admin")
MAX_QUERY = 1000
MAX_BODY = 1_000_000
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_VIEWS_PER_SOURCE = 20_000
DEFAULT_FLOOR = 0.40  # only sets the 'response confidence' category for a business with no calibrated threshold: it decides no answer
PIPELINE_MODES = shadow.MODES


SECRET_FILE = re.compile(r"(^|[\\/])\.env($|\.)|\.env$|\.(pem|key|p12|pfx|keystore|jks)$|(^|[\\/])id_(rsa|dsa|ecdsa|ed25519)|(^|[\\/])(credentials|secrets?)(\.[a-z]+)?$", re.I)


class ServiceError(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


def what_it_says_about_itself(kb) -> dict:
    """The dates a source states for itself and the sources it replaces (from its front-matter), in the form the database stores. A date that is not a date, or an end before the start, is
    left out with a note: nothing is guessed, and a mistake in a file's header never stops its text from being read."""
    out = {k: next((getattr(e, k) for e in kb.entries if getattr(e, k, None)), None) for k in ("valid_from", "valid_until", "supersedes")}
    start, end = parse_date(out["valid_from"]), parse_date(out["valid_until"])
    for k, d in (("valid_from", start), ("valid_until", end)):
        if out[k] and d is None:
            kb.warnings.append(f"{k.replace('_', ' ')}: \"{out[k]}\" is not a date (write it as 2026-12-31); it was ignored")
    if start and end and start > end:
        kb.warnings.append(f"the source starts ({start}) after it ends ({end}); both dates were ignored")
        start = end = None
    return {"valid_from": start.isoformat() if start else None, "valid_until": end.isoformat() if end else None, "supersedes": (out["supersedes"] or "")[:1000] or None}


class Tenant:
    """One business's searchable knowledge, in memory."""

    def __init__(self, token, index: HybridIndex, db_ids: dict[str, str]):
        self.token, self.index, self.db_ids = token, index, db_ids


class StorageClient:
    """Reads uploaded files from Supabase Storage with the service-role key (server side only)."""

    def __init__(self, url: str, service_key: str, bucket: str = "knowledge-files"):
        self.url, self.key, self.bucket = url.rstrip("/"), service_key, bucket

    def __call__(self, path: str) -> bytes:
        req = urllib.request.Request(f"{self.url}/storage/v1/object/{self.bucket}/{urllib.parse.quote(path)}", headers={"Authorization": f"Bearer {self.key}", "apikey": self.key})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - the address is the project's own, from configuration
                data = r.read(MAX_FILE_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise ServiceError(404 if e.code in (400, 404) else 502, "the uploaded file could not be read from storage") from None
        except OSError:
            raise ServiceError(502, "storage is not reachable") from None
        if len(data) > MAX_FILE_BYTES:
            raise ServiceError(413, "the file is larger than 25 MB")
        return data


class HostedEngine:
    def __init__(self, store: Store, embedder: Embedder, secrets: SecretBox | None = None, fetch_object=None, allow_private_llm: bool = False, cache_size: int = 16,
                 rate_per_minute: int = 60, model_name: str = "minilm-l6", today=date.today, pipeline_mode: str = "off", provider_factory=None, shadow_sample: float = 1.0, shadow_records_per_hour: int = 50, rng=random.random,
                 ai_modes: frozenset[str] | set[str] = frozenset(), platform: registry.PlatformCredentials | None = None, breakers: Breakers | None = None, inference_per_minute: int = 20,
                 max_platform_concurrent: int = 8, pending_ttl: float = 180.0, scanner=None, scan_required: bool = False, fast_path: bool = True):
        if pipeline_mode not in PIPELINE_MODES:
            raise ValueError(f"pipeline_mode must be one of {PIPELINE_MODES}")
        self.store, self.embedder, self.secrets, self.fetch_object = store, embedder, secrets, fetch_object
        self.fast_path = fast_path  # ENGINE_FAST_PATH: the conservative deterministic fast path (an exact, verified answer is not sent to a model). The operator's switch; no customer setting changes with it
        self.pipeline_mode = pipeline_mode  # ENGINE_PIPELINE; changing it (and restarting) is the whole rollout and the whole rollback
        self.provider_factory = provider_factory  # business id -> a contracts.Provider or None. Nothing in this product sets it: no external inference provider is connected. Tests use it to prove the consent checks
        self.stats: Counter = Counter()  # counts only: pipeline answers, shadow matches and mismatches (and their kinds), errors, fallbacks to the existing path
        if not 0.0 <= shadow_sample <= 1.0:
            raise ValueError("shadow_sample must be between 0 and 1")
        self.shadow_sample, self._rng = shadow_sample, rng  # the share of requests the pipeline is also run on in shadow mode (ENGINE_SHADOW_SAMPLE): the cost of a controlled test is chosen, not inherited
        self.shadow_records_per_hour, self._recorded = shadow_records_per_hour, {}  # at most this many shadow records per business per hour reach the audit log; the rest are only counted
        self.latency: dict[str, deque] = {"legacy": deque(maxlen=5000), "pipeline": deque(maxlen=5000)}  # milliseconds of the last requests, per path
        self.allow_private_llm, self.model_name, self.rate = allow_private_llm, model_name, rate_per_minute
        bad = set(ai_modes) - set(registry.AI_MODES)
        if bad:
            raise ValueError(f"ai_modes may only name {registry.AI_MODES}: {sorted(bad)}")
        self.ai_modes = frozenset(ai_modes)  # ENGINE_AI_MODES: the operator's switch. Empty (the default) means no business can use any AI mode, whatever it has configured or agreed to
        self.tele = telemetry.Metrics()
        self.breakers = breakers or Breakers()
        self.limits = Limits(store, inference_per_minute, max_platform_concurrent)
        self.router = Router(store, secrets, platform or registry.PlatformCredentials(), self.breakers, self.limits, self.ai_modes, allow_private=allow_private_llm)
        self.scanner, self.scan_required = scanner or NullScanner(), scan_required  # uploaded files are scanned before they are read when a scanner is configured; scan_required refuses what could not be scanned
        self._pending: OrderedDict[str, tuple] = OrderedDict()  # local mode: requests whose model call is being made on the person's own machine (single use, short-lived, per business and person)
        self.pending_ttl = pending_ttl
        self._tenants: OrderedDict[str, Tenant] = OrderedDict()
        self._cache_size = cache_size
        self.today = today  # what "today" is for expiry and replacement: a clock the tests can set
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    # ---- guards
    def _require(self, user_id: str, business_id: str, roles: tuple[str, ...]) -> str:
        if not is_uuid(user_id) or not is_uuid(business_id):
            raise ServiceError(400, "business_id and user_id must be ids")
        role = self.store.role_of(user_id, business_id)
        if role is None:
            self.tele.inc("security_denied")
            raise ServiceError(403, "you do not have access to this business")  # the same answer whether it exists or not
        if role not in roles:
            self.tele.inc("security_denied")
            raise ServiceError(403, "your role does not allow this")
        return role

    def _limit(self, business_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(business_id, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.rate:
                raise ServiceError(429, "too many questions in a minute; try again shortly", retry_after=int(60 - (now - q[0])) + 1)
            q.append(now)

    # ---- the index
    def _tenant(self, business_id: str) -> Tenant | None:
        token = (*self.store.index_token(business_id), self.today().isoformat())  # the day is part of it: a source expires at midnight without anyone changing it
        with self._lock:
            t = self._tenants.get(business_id)
            if t is not None and t.token == token:
                self._tenants.move_to_end(business_id)
                return t
        data = self.store.load_index(business_id)
        if not data["entries"]:
            with self._lock:
                self._tenants.pop(business_id, None)
            return None
        why = inactive_reasons([{**e, "source": e["source_name"]} for e in data["entries"]], self.today())  # expired, not yet valid or replaced by another source: kept out of the search, but a question only they answer is told so
        for e in data["entries"]:
            if e["id"] in why:
                e["inactive"] = why[e["id"]]
        views, vectors = list(data["views"]), data["vectors"]
        if data["learned"]:  # questions people approved earlier are searched as extra ways of asking their entry
            vecs = self.embedder.embed([q for _, q in data["learned"]], "doc")
            views += [{"text": q, "entry": i, "kind": "q"} for i, q in data["learned"]]
            vectors = np.concatenate([vectors, vecs])
            order = sorted(range(len(views)), key=lambda k: views[k]["entry"])  # stable: an entry's own views stay first
            views, vectors = [views[k] for k in order], vectors[order]
        t = Tenant(token, HybridIndex(data["entries"], views, vectors), data["db_ids"])
        with self._lock:
            self._tenants[business_id] = t
            while len(self._tenants) > self._cache_size:
                self._tenants.popitem(last=False)
        return t

    # ---- answering
    def _search_for(self, tenant: Tenant, settings: dict):
        method = settings["retrieval_method"]
        alpha = settings["hybrid_alpha"] if settings["hybrid_alpha"] is not None else DEFAULT_ALPHA

        def search(text: str, on=None):
            started = time.perf_counter()
            qv = self.embedder.embed([text], "query")[0]
            embed_ms = (time.perf_counter() - started) * 1000
            faq, passages, diag = tenant.index.search(text, qv, CANDIDATES, method, alpha, on=on)
            diag["embed_ms"] = embed_ms
            return faq, passages, diag

        search.model = tenant.index.fact_model  # the rows of tables, for questions that compare them
        return search

    def _run_pipeline(self, business_id: str, q: str, search, sim, floor: float, sem_min: float) -> dict:
        """The deterministic pipeline for one request (the flag's `shadow` and `on`): no provider, nothing sent anywhere."""
        result = Pipeline("deterministic", tenant_id=business_id).run(q, search, sim, as_of=self.today(), similarity_floor=floor, sem_min=sem_min)
        result["execution"] = {"path": "pipeline", **result["execution"]}
        return result

    def _may_record(self, business_id: str, kind: str) -> bool:
        """A small allowance of audit records per business and hour for shadow differences and faults: an unusual burst of them must not become a burst of writes."""
        now = time.monotonic()
        with self._lock:
            q = self._recorded.setdefault((business_id, kind), deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.shadow_records_per_hour:
                self.stats["shadow_unrecorded"] += 1
                return False
            q.append(now)
            return True

    def metrics(self) -> dict:
        """What GET /v1/metrics reports: the pipeline's rollout counters and times (as before), plus the service's telemetry, the state of every circuit, and which operator switches are on. No business, no
        person, no text: the busiest businesses are listed by id for the operator."""
        def summary(values) -> dict:
            v = list(values)
            return {"n": len(v), "median_ms": None if not v else round(shadow.percentile(v, 50) or 0.0, 1), "p95_ms": None if not v else round(shadow.percentile(v, 95) or 0.0, 1)}
        return {"mode": self.pipeline_mode, "shadow_sample": self.shadow_sample, **dict(self.stats), "latency": {k: summary(v) for k, v in self.latency.items()},
                "ai_modes_switched_on": sorted(self.ai_modes), "platform_credentials": self.router.platform.status(), "circuits": self.breakers.states(), "telemetry": self.tele.snapshot()}

    def _shadow(self, business_id: str, user_id: str, q: str, legacy: dict, new: dict) -> None:
        """The existing result and the pipeline's, for the same request, compared. Differences are recorded as short phrases and ids; no question text, no document text, and nothing is returned."""
        diffs = shadow.compare(legacy, new)
        other = {e.get("tenant") for e in new.get("evidence") or []} - {None, business_id}
        if other:
            diffs.append("tenant context differs: evidence from another business")
        if not diffs:
            self.stats["shadow_match"] += 1
            return
        self.stats["shadow_mismatch"] += 1
        kinds = shadow.categories(diffs)
        for k in kinds:
            self.stats[f"shadow_diff_{k}"] += 1
        if not self._may_record(business_id, "mismatch"):
            return
        try:
            self.store.record_audit(business_id, user_id, "pipeline.shadow_mismatch", "request", hashlib.sha256(q.lower().encode()).hexdigest()[:16],
                                    {"differences": diffs[:20], "categories": kinds, "legacy_outcome": legacy.get("outcome"), "pipeline_outcome": new.get("outcome"), "as_of": self.today().isoformat(),
                                     "pipeline_ms": (new.get("execution") or {}).get("stage_ms", {}).get("total")})
        except Exception:  # noqa: BLE001 - recording a difference must never fail the answer
            log.exception("a shadow difference could not be recorded for business %s", business_id)

    # ------------------------------------------------------------------ answering
    def respond(self, business_id: str, user_id: str, query: str, conversation_id: str | None = None, correlation_id: str | None = None) -> dict:
        cid = telemetry.clean_correlation(correlation_id) or telemetry.new_correlation_id()
        started = time.perf_counter()
        self._require(user_id, business_id, READ)
        q = normalize_query(query)
        if not q or len(q) > MAX_QUERY:
            raise ServiceError(400, f"the question must be 1 to {MAX_QUERY} characters")
        try:
            self._limit(business_id)
        except ServiceError:
            self.tele.inc("rate_limited", tenant=business_id)
            raise
        settings = self.store.settings(business_id)
        tenant = self._tenant(business_id)
        plan, injected = self._plan_for(business_id)
        route = plan.steps[0] if plan.steps else Route("deterministic", fallback=plan.fallback)
        result: dict
        if tenant is None:
            result = answer.respond(q, lambda _: ([], [], {"method": "none", "note": "this business has no searchable knowledge yet"}), None, llm=None)
            result["message"] = "There is no knowledge to search yet. Add a FAQ or a document first."
            result["execution"] = shadow.legacy_execution(result, None, None, "no knowledge")
        else:
            search = self._search_for(tenant, settings)

            def sim(part: str, texts: list[str]) -> list[float]:
                return (self.embedder.embed_docs_cached(texts) @ self.embedder.embed([part], "query")[0]).tolist() if texts else []

            floor = float(settings["thresholds"].get("floor", DEFAULT_FLOOR))
            sem_min = float(settings["thresholds"].get("evidence_sim_min", evidence.SEM_MIN))
            if not plan.steps:
                result = self._respond_deterministic(business_id, user_id, q, search, sim, floor, sem_min)
            else:
                result = self._respond_chain(business_id, q, search, sim, floor, sem_min, plan.steps, cid, injected)
            for sc in result.get("sources") or []:
                sc["db_id"] = tenant.db_ids.get(sc.get("id"))
        return self._finalize(business_id, user_id, q, conversation_id, result, settings, route, cid, started)

    def _route(self, business_id: str) -> tuple[Route, object | None]:
        """The decision for this business now, and an injected provider if there is one. A test can inject a provider (`provider_factory`, which nothing in the product sets); otherwise the router
        decides, from the database alone."""
        if self.provider_factory is not None:
            injected = self.provider_factory(business_id)
            if injected is not None:
                return Route("platform", injected.name, getattr(injected, "model", None), "deterministic"), injected
        return self.router.route(business_id), None

    def _plan_for(self, business_id: str) -> tuple[Plan, object | None]:
        """The business's steps now (see `Router.plan`), and an injected provider if a test set one: that is a single platform step, with nothing after it."""
        if self.provider_factory is not None:
            injected = self.provider_factory(business_id)
            if injected is not None:
                return Plan((Route("platform", injected.name, getattr(injected, "model", None), "deterministic", tier="paid"),), "deterministic"), injected
        return self.router.plan(business_id), None

    def _respond_deterministic(self, business_id: str, user_id: str, q: str, search, sim, floor: float, sem_min: float) -> dict:
        """The deterministic business: the existing path, the pipeline, or both with a comparison, as the flag says. Nothing here reaches a provider."""
        use_pipeline = self.pipeline_mode in ("shadow", "on")
        if use_pipeline and self.pipeline_mode == "shadow" and self._rng() >= self.shadow_sample:
            use_pipeline = False  # not in the sample: this request is answered and costs exactly what it did before
            self.stats["shadow_sampled_out"] += 1
        new: dict | None = None
        note: str | None = None
        if use_pipeline:
            started = time.perf_counter()
            try:
                new = self._run_pipeline(business_id, q, search, sim, floor, sem_min)
                self.latency["pipeline"].append((time.perf_counter() - started) * 1000)
                ex = new.get("execution") or {}
                if ex.get("fallback") or ex.get("generated_by") not in ("deterministic", "model"):
                    self.stats["pipeline_fallback"] += 1
            except Exception as e:  # noqa: BLE001 - a fault in the new path must not become a failed answer: the existing path answers, and the fault is counted
                self.stats["pipeline_error"] += 1
                self.tele.inc("pipeline_error", tenant=business_id)
                if self.pipeline_mode == "shadow":
                    self.stats["shadow_error"] += 1
                note = f"pipeline_error:{type(e).__name__}"
                log.error("the reasoning pipeline failed for business %s (%s); the existing path answered", business_id, type(e).__name__)
                self._record_fault(business_id, user_id, note)
        if self.pipeline_mode == "on" and new is not None:
            self.stats["pipeline_answered"] += 1
            return new
        started = time.perf_counter()
        result = answer.respond(q, search, sim, None, floor, sem_min, as_of=self.today())
        self.latency["legacy"].append((time.perf_counter() - started) * 1000)
        result["execution"] = shadow.legacy_execution(result, None, None, note)
        self.stats["legacy_answered"] += 1
        if self.pipeline_mode == "shadow" and new is not None:
            self._shadow(business_id, user_id, q, result, new)
        return result

    UNAVAILABLE = ("provider:", "consent:", "limit:", "config:", "local:")

    def _run_step(self, business_id: str, q: str, search, sim, floor: float, sem_min: float, route: Route, injected: object | None = None) -> dict:
        """One step of the plan: the SAME pipeline as every other mode, composed by whatever this route allows. A route that was refused composes nothing and says why."""
        slot = False
        refusal = route.refusal
        if route.ok and injected is None and route.mode == "platform":
            slot = self.limits.take_platform_slot()  # the platform's shared capacity is not unlimited either
            if not slot:
                refusal = "limit:platform_busy"
        try:
            provider: object
            if route.client_side:
                provider = Refused(route.provider or "local", "local:client_inference_required", route.model or "")  # this client did not run the local model: the deterministic answer, said as such
            elif refusal:
                provider = Refused(route.provider or route.mode, refusal, route.model or "")
            elif injected is not None:
                provider = injected
            else:
                try:
                    provider = self.router.provider(business_id, route)
                except RouteError as e:
                    refusal = e.reason
                    provider = Refused(route.provider or route.mode, e.reason, route.model or "")
            consent = DbConsent(self.store, business_id) if registry.third_party(route.provider or "") else None
            pipe = Pipeline(route.mode, provider, consent, allow_fallback=route.fallback == "deterministic", tenant_id=business_id, fast_path=self.fast_path)  # type: ignore[arg-type]
            result = pipe.run(q, search, sim, as_of=self.today(), similarity_floor=floor, sem_min=sem_min)
        finally:
            if slot:
                self.limits.release_platform_slot()
        fast = result["execution"].get("answer_path") == "fast_path"  # answered without a model: nothing is left for the person's computer to do
        result["execution"] = {"path": "pipeline", **result["execution"], "tier": route.tier, **({"local": {"available": True, "route": "/v1/local/prepare"}} if route.client_side and not fast else {})}
        return result

    @classmethod
    def _outcome(cls, ex: dict) -> str:
        """What a step did, for the next decision: `answered` (a model's answer was used), `unavailable` (the step could not be used: refused, or the provider failed: the next permitted step may be
        tried), `reasoning_failed` (a model answered but its claims did not verify: NOT a reason to send the evidence to another provider), `cancelled` (the person stopped it: nothing else is tried) or `no_model_needed`."""
        failure = str(ex.get("failure_kind") or "")
        if ex.get("generated_by") == "model":
            return "answered"
        if failure == "provider:cancelled":
            return "cancelled"  # the person stopped it: that is not a step failing, and nothing is tried instead
        if failure.startswith(cls.UNAVAILABLE):
            return "unavailable"
        if failure.startswith("reasoning:"):
            return "reasoning_failed"
        return "no_model_needed"

    @staticmethod
    def _attempt(route: Route, outcome: str, reason: str | None, ex: dict | None = None) -> dict:
        usage = (ex or {}).get("usage") or None
        return {"step": route.tier or route.mode, "mode": route.mode, "tier": route.tier, "provider": route.provider, "model": route.model, "outcome": outcome, "reason": reason,
                "usage": usage if usage and usage.get("attempts") else None}

    def _respond_chain(self, business_id: str, q: str, search, sim, floor: float, sem_min: float, steps: tuple[Route, ...], cid: str, injected: object | None = None,
                       earlier: list[dict] | None = None) -> dict:
        """Walks the business's steps in order and stops at the first that answers. A step that is refused (no consent, no quota, circuit open, no companion) is skipped without anything being sent;
        a step whose provider fails hands over to the next step the business ENABLED; a step whose model answered but failed verification does not (a bad answer is not a reason to send the
        evidence somewhere else). The step that local inference needs the person's own browser for is returned as a hint and nothing is sent: the browser runs it (`/v1/local/prepare`).
        Every step is recorded in `execution.attempts`, with what it cost."""
        attempts: list[dict] = list(earlier or [])
        result: dict | None = None
        for idx, route in enumerate(steps):
            if route.client_side and route.ok:
                result = self._run_step(business_id, q, search, sim, floor, sem_min, route, injected)
                if result["execution"].get("answer_path") == "fast_path":
                    attempts.append(self._attempt(route, "no_model_needed", f"fast_path:{result['execution'].get('fast_path_reason')}"))
                else:
                    attempts.append(self._attempt(route, "waiting_for_the_computer", "local:client_inference_required"))
                break
            if not route.ok:
                attempts.append(self._attempt(route, "refused", route.refusal))
                continue
            result = self._run_step(business_id, q, search, sim, floor, sem_min, route, injected)
            ex = result["execution"]
            outcome = self._outcome(ex)
            attempts.append(self._attempt(route, outcome, ex.get("failure_kind"), ex))
            if outcome != "unavailable":
                break
        if result is None:  # every step was refused: say why, in the words of the preferred step, and send nothing
            result = self._run_step(business_id, q, search, sim, floor, sem_min, steps[0], injected)
        ex = result["execution"]
        ex["attempts"] = attempts
        ex["provider_steps_tried"] = [a["step"] for a in attempts if a.get("usage")]  # the steps that actually reached a provider or the person's computer (a refused step, or one that was not needed, did not)
        answered = [a for a in attempts if a["outcome"] == "answered"]
        if answered and attempts[0] is not answered[0]:
            first = attempts[0]
            ex["fallback_from"] = {"step": first["step"], "provider": first["provider"], "reason": first["reason"]}  # who would have answered, and why somebody else did
        return result

    def _finalize(self, business_id: str, user_id: str, q: str, conversation_id: str | None, result: dict, settings: dict, route: Route, cid: str, started: float) -> dict:
        """What every answer gets, whichever path made it: the correlation id, the exchange stored (if the business keeps questions), usage metered per mode, counters, and one structured log line
        with ids, kinds and times and nothing that was said."""
        ex = result.setdefault("execution", {})
        ex["correlation_id"] = cid
        elapsed = (time.perf_counter() - started) * 1000
        out: dict = {"result": result, "conversation_id": None, "message_id": None}
        if settings["privacy"].get("log_questions", True):
            out["conversation_id"], out["message_id"] = self.store.record_exchange(business_id, user_id, conversation_id, q, result)
        final_mode = ex.get("mode") if ex.get("mode") in registry.MODES else route.mode  # the step that produced the answer (it may not be the first one)
        mode = final_mode if ex.get("path") == "pipeline" or final_mode != "deterministic" else "deterministic"
        self.store.record_usage(business_id, "question", out["message_id"], latency_ms=int(result.get("latency_ms") or elapsed), status="fallback" if ex.get("fallback") else "ok", mode=mode, correlation_id=cid,
                                generated_by=ex.get("generated_by"), failure_kind=ex.get("failure_kind"), tier=ex.get("tier"))
        usage = ex.get("usage") or {}
        failure = str(ex.get("failure_kind") or "")
        # every provider that was asked, whether or not its answer was used, is metered against its own step, so that quotas and spending see every call
        calls = [(a["mode"], a.get("tier"), a["provider"], a["model"], a["usage"], a["outcome"], a.get("reason") or "") for a in ex.get("attempts") or [] if a.get("usage")]
        if not calls and usage.get("attempts"):
            calls = [(route.mode, route.tier, ex.get("provider"), ex.get("model"), usage, "", failure)]
        for c_mode, c_tier, c_provider, c_model, u, c_outcome, c_reason in calls:
            is_failed = str(c_reason).startswith("provider:")
            self.store.record_usage(business_id, "llm_call", out["message_id"], c_provider, c_model, int(u.get("tokens_in") or 0), int(u.get("tokens_out") or 0), u.get("cost"),
                                    int(u.get("provider_ms") or 0), "failed" if is_failed else "fallback" if (ex.get("fallback") and not c_outcome) or c_outcome == "reasoning_failed" else "ok", mode=c_mode,
                                    correlation_id=cid, generated_by=ex.get("generated_by"), failure_kind=c_reason or None, tier=c_tier)
        t = self.tele
        t.inc("request", tenant=business_id)
        t.inc(f"outcome_{result.get('outcome')}")
        t.inc(f"generated_by_{ex.get('generated_by')}")
        if ex.get("answer_path"):
            t.inc(f"answer_path_{ex['answer_path']}")
        t.observe("request_ms", elapsed)
        for stage in ("retrieve_normalize", "validate", "engine_compose"):
            if stage in (ex.get("stage_ms") or {}):
                t.observe(f"stage_{stage}_ms", ex["stage_ms"][stage])
        for _m, _t, _p, _mo, u, c_outcome, c_reason in calls:
            t.inc("provider_call", tenant=business_id)
            t.observe("provider_ms", u.get("provider_ms") or 0)
            t.inc("tokens_in", int(u.get("tokens_in") or 0))
            t.inc("tokens_out", int(u.get("tokens_out") or 0))
            if str(c_reason).startswith("provider:"):
                t.inc("provider_failure", tenant=business_id)
            else:  # a model answered (its answer may still have been refused): count it, and count the ones that lost a claim to verification
                t.inc("model_answer")
                if str(c_reason).startswith("reasoning:") or ex.get("validation") == "partly_verified":
                    t.inc("claims_rejected")
        if len(ex.get("attempts") or []) > 1 and ex.get("fallback_from"):
            t.inc("step_fallback", tenant=business_id)
        reasons = [str(a.get("reason") or "") for a in ex.get("attempts") or [] if a.get("outcome") == "refused"] or [failure]  # every step that was refused counts, not only the last
        for why in reasons:
            if why.startswith("consent:"):
                t.inc("consent_refused", tenant=business_id)
            elif why.startswith("limit:"):
                t.inc("quota_refused" if "quota" in why or why in ("limit:no_plan", "limit:trial_ended", "limit:no_free_quota", "limit:spend_cap") else "rate_limited", tenant=business_id)
        telemetry.log_event(log, "respond", correlation_id=cid, business=business_id, mode=mode, tier=ex.get("tier"), steps=len(ex.get("attempts") or []), provider=ex.get("provider"), model=ex.get("model"), outcome=result.get("outcome"),
                            generated_by=ex.get("generated_by"), answer_path=ex.get("answer_path"), failure_kind=failure or None, request_ms=round(elapsed, 1), tokens_in=usage.get("tokens_in"), tokens_out=usage.get("tokens_out"),
                            cost=usage.get("cost"), path=ex.get("path"))
        return out

    def _record_fault(self, business_id: str, user_id: str, note: str) -> None:
        if not self._may_record(business_id, "fault"):
            return
        try:
            self.store.record_audit(business_id, user_id, "pipeline.error", "request", None, {"error": note, "fell_back_to": "legacy"})
        except Exception:  # noqa: BLE001
            log.exception("a pipeline fault could not be recorded for business %s", business_id)

    # ---- agreements to send data to an AI provider
    def disclosure_for(self, business_id: str, user_id: str, provider: str) -> dict:
        self._require(user_id, business_id, READ)
        if not isinstance(provider, str) or not disclosure.known(provider):
            raise ServiceError(400, "choose a provider that has a disclosure")
        return disclosure.describe(provider)

    def consent_get(self, business_id: str, user_id: str) -> dict:
        """Every provider that has a disclosure: whether its terms are verified (only then can it be agreed to), whether this business has agreed to the CURRENT text, and the history (who agreed
        or withdrew, when, to which version). A withdrawal is always possible, verified or not."""
        self._require(user_id, business_id, READ)
        history = self.store.consents(business_id)
        out = []
        for provider, entry in disclosure.providers().items():
            current = disclosure.current_version(provider)
            mine = [h for h in history if h["provider"] == provider]
            v = disclosure.verification(provider)
            agreed = any(h["status"] == "granted" and h["disclosure_version"] == current for h in mine)
            out.append({"provider": provider, "label": entry["label"], "disclosure_version": current, "terms_verified": v["status"] == "verified", "consentable": v["status"] == "verified",
                        "unverified": v["missing"] + v["problems"], "agreed": agreed, "in_use": agreed and v["status"] == "verified", "history": mine})
        return {"providers": out, "external_inference": {"connected": False, "note": "no external inference provider is connected to this service; an agreement is recorded now and used only when one is"}}

    def consent_grant(self, business_id: str, user_id: str, provider: str, version: str) -> dict:
        self._require(user_id, business_id, ADMIN)
        if not isinstance(provider, str) or not disclosure.known(provider):
            raise ServiceError(400, "choose a provider that has a disclosure")
        if not disclosure.verified(provider):
            raise ServiceError(409, "this provider's terms have not been verified, so it cannot be agreed to yet")
        if version != disclosure.current_version(provider):
            raise ServiceError(409, "the disclosure text has changed or was not the one shown; read the current text and agree to that")
        self.store.grant_consent(business_id, provider, version, disclosure.sha256(provider), user_id, ["question", "evidence_passages"])
        return self.consent_get(business_id, user_id)

    def consent_revoke(self, business_id: str, user_id: str, provider: str) -> dict:
        self._require(user_id, business_id, ADMIN)
        if not isinstance(provider, str) or not disclosure.known(provider):
            raise ServiceError(400, "choose a provider that has a disclosure")
        self.store.revoke_consent(business_id, provider, user_id)
        return self.consent_get(business_id, user_id)

    # ------------------------------------------------------------------ how a business answers
    # what a person chooses on the setup screen: the first step and the steps that may follow it. Every follower is OFF unless the person turned it on.
    POLICIES = {
        "deterministic_only": {"mode": "deterministic"},
        "local_only": {"mode": "local"},
        "local_free": {"mode": "local", "fallback_free": True},
        "local_free_paid": {"mode": "local", "fallback_free": True, "fallback_paid": True},
        "byok_preferred": {"mode": "byok"},
        "byok_alternative": {"mode": "local", "fallback_byok": True},
    }
    FLAGS = ("fallback_byok", "fallback_free", "fallback_paid")

    @classmethod
    def policy_name(cls, s: dict) -> str:
        """The name of the settings if they are one of the setup screen's choices (`custom` otherwise)."""
        flags = {f: bool(s.get(f)) for f in cls.FLAGS}
        for name, p in cls.POLICIES.items():
            if p["mode"] == s["mode"] and all(flags[f] == bool(p.get(f)) for f in cls.FLAGS):
                return name
        return "custom"

    def ai_settings_get(self, business_id: str, user_id: str) -> dict:
        self._require(user_id, business_id, READ)
        s = self.store.ai_settings(business_id)
        plan = self.router.plan(business_id)
        r = plan.steps[0] if plan.steps else Route("deterministic", fallback=plan.fallback)
        offered = [{"provider": m.provider, "model": m.id, "label": m.label, "tier": m.tier, "price_in_per_m": m.price_in_per_m, "price_out_per_m": m.price_out_per_m}
                   for m in catalogue.platform_models() if self.router.platform.available(m.provider) and disclosure.verified(m.provider)]
        hosted = {}
        for tier in ("free", "paid"):
            info, why = self.router.hosted_model(tier)
            hosted[tier] = {"available": info is not None and why is None, "reason": why, "provider": info.provider if info else None, "model": info.id if info else None,
                            "label": info.label if info else None, "price_in_per_m": info.price_in_per_m if info else None, "price_out_per_m": info.price_out_per_m if info else None}
        steps = [{"step": x.tier or x.mode, "mode": x.mode, "provider": x.provider, "model": x.model, "usable": x.ok, "refusal": x.refusal} for x in plan.steps]
        return {"settings": s, "policy": self.policy_name(s), "route": {"mode": r.mode, "usable": r.ok, "refusal": r.refusal, "provider": r.provider, "model": r.model}, "steps": steps,
                "hosted": hosted, "switched_on_by_the_operator": sorted(self.ai_modes), "platform_models": offered, "companions": self.store.companions(business_id)}

    def ai_settings_save(self, business_id: str, user_id: str, body: dict) -> dict:
        """The business's choice of how to answer: the first step and the steps that may follow it. The choice is validated here against what exists: a mode the operator switched on, a platform model
        that was released on purpose, a provider the business has set up, a companion it has paired. A step that sends evidence outside the business (its own provider account, free hosted, paid
        hosted) can be turned on only when its provider's terms are verified AND the business has already agreed to them, and the paid step only with a spending cap. Nothing a browser names is
        trusted: the provider and model of a hosted step are looked up, not believed."""
        self._require(user_id, business_id, ADMIN)
        policy = body.get("policy")
        if policy is not None:
            preset = self.POLICIES.get(policy) if isinstance(policy, str) else None
            if preset is None:
                raise ServiceError(400, "choose one of: " + ", ".join(self.POLICIES))
            body = {**{f: bool(preset.get(f)) for f in self.FLAGS}, **{k: v for k, v in body.items() if k != "policy"}, "mode": preset["mode"]}  # a preset sets the first step; flags named beside it refine it
        mode = body.get("mode")
        if mode not in registry.MODES:
            raise ServiceError(400, "choose deterministic, platform, byok or local")
        current = self.store.ai_settings(business_id)
        fallback = body.get("fallback", current["fallback"])
        if fallback not in ("deterministic", "none"):
            raise ServiceError(400, "fallback must be deterministic or none")
        flags = {f: body.get(f, current[f]) for f in self.FLAGS}
        if any(not isinstance(v, bool) for v in flags.values()):
            raise ServiceError(400, "the steps after the first are on or off")
        if mode == "deterministic":
            flags = {f: False for f in self.FLAGS}  # deterministic means nothing else is ever tried
        cap = body.get("paid_cap_usd", current["paid_cap_usd"])
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= 1_000_000):
            raise ServiceError(400, "the monthly spending limit is an amount above zero")
        provider = model = None
        if mode != "deterministic" and mode not in self.ai_modes:
            raise ServiceError(409, "this mode is not switched on for this service")
        if mode == "platform":
            provider, model = body.get("platform_provider"), body.get("platform_model")
            info = catalogue.lookup(provider, model) if isinstance(provider, str) and isinstance(model, str) else None
            if info is None or not info.platform_offered:
                raise ServiceError(409, "that model is not offered")
        elif mode == "byok":
            cfg = self.store.llm_config(business_id)
            if not cfg or not cfg["provider"] or not cfg["model"] or not registry.serves(cfg["provider"], "byok"):
                raise ServiceError(409, "set up your provider and model first")
        elif mode == "local" and not self.store.active_companion(business_id):
            raise ServiceError(409, "pair a companion first")
        consent = DbConsent(self.store, business_id)

        def agreed(provider_name: str | None, what: str) -> None:
            if not provider_name or not disclosure.known(provider_name):
                raise ServiceError(409, f"{what} is not available")
            if not disclosure.verified(provider_name):
                raise ServiceError(409, f"{what} cannot be turned on: its provider's data-handling terms have not been verified by this service")
            if not consent.allows(business_id, provider_name):
                raise ServiceError(409, f"{what} cannot be turned on until your business has agreed to send data to {disclosure.describe(provider_name)['label']} (Settings, AI providers and your data)")

        if flags["fallback_byok"] and mode != "byok":
            if "byok" not in self.ai_modes:
                raise ServiceError(409, "your own provider account is not switched on for this service")
            cfg = self.store.llm_config(business_id)
            if not cfg or not cfg["enabled"] or not cfg["provider"] or not cfg["model"] or not registry.serves(cfg["provider"], "byok"):
                raise ServiceError(409, "set up your provider and model first")
            agreed(cfg["provider"], "your own provider account as an alternative")
        for tier, label in (("free", "free hosted inference"), ("paid", "paid hosted inference")):
            if flags[f"fallback_{tier}"]:
                if "platform" not in self.ai_modes:
                    raise ServiceError(409, f"{label} is not switched on for this service")
                info, why = self.router.hosted_model(tier)
                if info is None or why:
                    raise ServiceError(409, f"{label} is not available right now")
                agreed(info.provider, label)
        if flags["fallback_paid"] and cap is None:
            raise ServiceError(400, "paid hosted inference needs a monthly spending limit")
        self.store.save_ai_settings(business_id, user_id, mode, provider, model, fallback, flags["fallback_byok"], flags["fallback_free"], flags["fallback_paid"], float(cap) if cap is not None else None)
        return self.ai_settings_get(business_id, user_id)

    def usage_get(self, business_id: str, user_id: str) -> dict:
        self._require(user_id, business_id, ADMIN)
        since = datetime.now(timezone.utc) - timedelta(days=30)
        return {"summary": self.limits.summary(business_id, self.store.ai_settings(business_id)["paid_cap_usd"]), "by_day": self.store.usage_by_day(business_id, since), "per_minute_default": self.limits.default_per_minute}

    def history_get(self, business_id: str, user_id: str, limit: object = 50, before: object = None) -> dict:
        self._require(user_id, business_id, READ)
        n = limit if isinstance(limit, int) and not isinstance(limit, bool) else 50
        when = None
        if before is not None:
            try:
                when = datetime.fromisoformat(str(before))
            except ValueError:
                raise ServiceError(400, "before must be a time like 2026-11-02T10:00:00+00:00") from None
        return {"items": self.store.history(business_id, n, when)}

    # ---- companions on the business's own machines
    def companions_get(self, business_id: str, user_id: str) -> dict:
        self._require(user_id, business_id, READ)
        return {"companions": self.store.companions(business_id)}

    def companion_register(self, business_id: str, user_id: str, body: dict) -> dict:
        self._require(user_id, business_id, ADMIN)
        label, platform_, runtime, model = body.get("label"), body.get("platform"), body.get("runtime"), body.get("model")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or not label.isprintable():
            raise ServiceError(400, "give the companion a short name")
        if runtime not in ("ollama", "llamacpp") or not isinstance(model, str) or not 1 <= len(model) <= 200 or not (platform_ is None or (isinstance(platform_, str) and len(platform_) <= 80)):
            raise ServiceError(400, "say which runtime and model it runs")
        if sum(not c["revoked"] for c in self.store.companions(business_id)) >= 5:
            raise ServiceError(409, "five companions are paired: remove one first")
        fit = body.get("fit")
        if fit is not None and fit not in ("comfortable", "possible", "not_advised", "unknown"):
            raise ServiceError(400, "fit is the companion's own verdict for this model on this computer")
        cid = self.store.register_companion(business_id, user_id, label.strip(), platform_, runtime, model, fit)
        return {"id": cid, "companions": self.store.companions(business_id)}

    def companion_revoke(self, business_id: str, user_id: str, companion_id: str) -> dict:
        self._require(user_id, business_id, ADMIN)
        if not is_uuid(companion_id):
            raise ServiceError(400, "companion_id must be an id")
        self.store.revoke_companion(business_id, user_id, companion_id)
        if self.store.ai_settings(business_id)["mode"] == "local" and not self.store.active_companion(business_id):
            s = self.store.ai_settings(business_id)
            self.store.save_ai_settings(business_id, user_id, "deterministic", None, None, s["fallback"])  # no companion left: back to the deterministic answer, said, not left half-configured
        return {"companions": self.store.companions(business_id)}

    # ---- what a person approves while testing goes back into retrieval
    def feedback_approve(self, business_id: str, user_id: str, faq_id: str, question: str) -> dict:
        """"This question is answered by this entry": the question is stored as one more way of asking it, and the business's index (rebuilt when its learned questions change) searches it from the
        next question on. An owner, admin or editor only. Nothing is trained and no model weight changes: retrieval improves because the question now has its own vector next to the entry's."""
        self._require(user_id, business_id, WRITE)
        if not is_uuid(faq_id):
            raise ServiceError(400, "faq_id must be an id")
        text = " ".join(str(question or "").split())
        if not 1 <= len(text) <= 300:
            raise ServiceError(400, "the question must be 1 to 300 characters")
        out = self.store.add_learned(business_id, user_id, faq_id, text)
        status = out["status"]
        if status == "no_such_faq":
            raise ServiceError(404, "that FAQ is not part of this business's current knowledge (only an FAQ entry can learn a question; a document passage cannot)")
        if status == "full":
            raise ServiceError(409, "this entry (or the business) has learned as many questions as it keeps: remove some, or add them to the FAQ itself")
        if status == "already_learned":
            raise ServiceError(409, "this question is already learned for that entry")
        if status == "is_faq":
            raise ServiceError(409, "that question is already an FAQ question, so it is found as it is")
        if status == "refused":
            raise ServiceError(409, "that question is already an FAQ question, or belongs to a test set, so it is not added: " + out["reason"])
        return {"learned": {"id": out["id"], "question": text, "faq_id": faq_id}, "total": out["total"], "for_this_entry": out["for_this_entry"]}

    def learned_list(self, business_id: str, user_id: str) -> dict:
        self._require(user_id, business_id, READ)
        return {"learned": self.store.learned_list(business_id)}

    def learned_remove(self, business_id: str, user_id: str, learned_id: str) -> dict:
        self._require(user_id, business_id, WRITE)
        if not is_uuid(learned_id):
            raise ServiceError(400, "learned_id must be an id")
        if not self.store.remove_learned(business_id, user_id, learned_id):
            raise ServiceError(404, "that learned question does not exist")
        return {"learned": self.store.learned_list(business_id)}

    # ---- local mode: the model call is made by the person's own browser, on the person's own machine
    LOCAL_FAILURES = frozenset({"network", "timeout", "not_found", "server", "cancelled", "not_connected", "auth", "bad_request"})

    def local_prepare(self, business_id: str, user_id: str, query: str, conversation_id: str | None = None, correlation_id: str | None = None) -> dict:
        """Phase one of a local-mode question: every stage up to composition runs here, on the business's own knowledge, and what the model must be given goes back to the person's browser, which
        passes it to their companion and nothing else. Nothing is sent to any provider. The evidence is held here under a one-use request id, so that what comes back is verified against exactly it."""
        cid = telemetry.clean_correlation(correlation_id) or telemetry.new_correlation_id()
        started = time.perf_counter()
        self._require(user_id, business_id, READ)
        q = normalize_query(query)
        if not q or len(q) > MAX_QUERY:
            raise ServiceError(400, f"the question must be 1 to {MAX_QUERY} characters")
        self._limit(business_id)
        plan = self.router.plan(business_id)
        route = plan.steps[0] if plan.steps else Route("deterministic")
        if route.mode != "local" or not route.ok:
            raise ServiceError(409, "this business is not set up for local answering")
        settings = self.store.settings(business_id)
        tenant = self._tenant(business_id)
        if tenant is None:
            result = answer.respond(q, lambda _: ([], [], {"method": "none", "note": "this business has no searchable knowledge yet"}), None, llm=None)
            result["message"] = "There is no knowledge to search yet. Add a FAQ or a document first."
            result["execution"] = shadow.legacy_execution(result, None, None, "no knowledge")
            return {"done": True, **self._finalize(business_id, user_id, q, conversation_id, result, settings, route, cid, started)}
        search = self._search_for(tenant, settings)

        def sim(part: str, texts: list[str]) -> list[float]:
            return (self.embedder.embed_docs_cached(texts) @ self.embedder.embed([part], "query")[0]).tolist() if texts else []

        pipe = Pipeline("local", ClientSide(route.provider or "local", route.model or ""), None, allow_fallback=route.fallback == "deterministic", tenant_id=business_id, fast_path=self.fast_path)  # type: ignore[arg-type]
        st = pipe.prepare(q, search, sim, as_of=self.today(), similarity_floor=float(settings["thresholds"].get("floor", DEFAULT_FLOOR)),
                          sem_min=float(settings["thresholds"].get("evidence_sim_min", evidence.SEM_MIN)))
        decision = pipe.evaluate_fast_path(st) if self.fast_path else None
        if decision is not None and decision.taken:  # a complete, verified answer already exists: the person's computer is not asked, and nothing leaves it
            result = pipe.conclude_fast(st, decision)
            result["execution"] = {"path": "pipeline", **result["execution"], "tier": "local", "attempts": [self._attempt(route, "no_model_needed", f"fast_path:{decision.reason}")], "provider_steps_tried": []}
            for sc in result.get("sources") or []:
                sc["db_id"] = tenant.db_ids.get(sc.get("id"))
            return {"done": True, **self._finalize(business_id, user_id, q, conversation_id, result, settings, route, cid, started)}
        if st.base.get("evidence_class") == "computed" or not st.usable:  # arithmetic is done in code, and with no usable evidence there is nothing to compose: no model is asked
            result = pipe.conclude_deterministic(st)
            result["execution"] = {"path": "pipeline", **result["execution"]}
            for sc in result.get("sources") or []:
                sc["db_id"] = tenant.db_ids.get(sc.get("id"))
            return {"done": True, **self._finalize(business_id, user_id, q, conversation_id, result, settings, route, cid, started)}
        rid = os.urandom(24).hex()
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._pending.items() if v[-1] < now]:
                del self._pending[k]
            if sum(v[2] == business_id for v in self._pending.values()) >= 20:
                raise ServiceError(429, "too many local questions are waiting for their answers", retry_after=10)
            while len(self._pending) >= 500:
                self._pending.popitem(last=False)
            self._pending[rid] = (st, pipe, business_id, user_id, q, conversation_id, cid, started, route, settings, tenant, now + self.pending_ttl)
        return {"done": False, "request_id": rid, "expires_in": int(self.pending_ttl), "correlation_id": cid, "companion": route.companion, "evidence_ids": [e.id for e in st.usable],
                "prompt": {"system": compose.SYSTEM, "user": compose.prompt(st.plan, st.usable), "schema": compose.SCHEMA}}

    def local_complete(self, business_id: str, user_id: str, request_id: object, text: object = None, failure: object = None, tokens_in: object = 0, tokens_out: object = 0) -> dict:
        """Phase two: what the person's companion returned (or why it could not). It is untrusted like any provider's text: parsed and checked claim by claim against the evidence held for this
        request. A request id works once, for the business and the person it was made for, before it expires."""
        self._require(user_id, business_id, READ)
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{48}", request_id):
            raise ServiceError(400, "request_id is not valid")
        if failure is not None and failure not in self.LOCAL_FAILURES:
            raise ServiceError(400, "failure is not one of the kinds that can be reported")
        if text is not None and (not isinstance(text, str) or len(text) > 200_000):
            raise ServiceError(400, "text must be a string of at most 200000 characters")
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is None or entry[-1] < time.monotonic():
                self._pending.pop(request_id, None)
                raise ServiceError(410, "this request has expired or was already answered: ask again")
            if entry[2] != business_id or entry[3] != user_id:
                self.tele.inc("security_denied")
                raise ServiceError(403, "this request was not made by you for this business")
            del self._pending[request_id]  # one use
        st, pipe, _, _, q, conversation_id, cid, started, route, settings, tenant, _ = entry
        ints = [min(max(int(x), 0), 10**7) if isinstance(x, int) and not isinstance(x, bool) else 0 for x in (tokens_in, tokens_out)]
        reply = ProviderReply(text=text or "", tokens_in=ints[0], tokens_out=ints[1], failure=f"provider:{failure}" if failure else (None if text else "provider:server"), attempts=1, model=route.model)
        result = pipe.conclude_with_reply(st, reply)
        result["execution"] = {"path": "pipeline", **result["execution"], "tier": "local"}
        local_attempt = self._attempt(route, self._outcome(result["execution"]), result["execution"].get("failure_kind"), result["execution"])
        result["execution"]["attempts"] = [local_attempt]
        result["execution"]["provider_steps_tried"] = ["local"] if local_attempt.get("usage") else []
        if (reply.failure or "").startswith("provider:") and reply.failure != "provider:cancelled":
            # the person's computer could not answer (not a bad answer: it did not answer): the steps the business ENABLED after it get their turn, each one only if its provider is verified, the
            # business agreed to it and its limits allow it. If none can be used, the answer stays what the computer's failure made it, and the refusals are listed.
            later = tuple(r for r in self.router.plan(business_id).steps if not r.client_side)
            if later:
                search, sim, floor, sem_min = self._search_context(tenant, settings)
                chain = self._respond_chain(business_id, q, search, sim, floor, sem_min, later, cid, None, earlier=[local_attempt])
                if any(a["outcome"] != "refused" for a in chain["execution"]["attempts"][1:]):
                    result = chain
                else:
                    result["execution"]["attempts"] = chain["execution"]["attempts"]
        for sc in result.get("sources") or []:
            sc["db_id"] = tenant.db_ids.get(sc.get("id"))
        return self._finalize(business_id, user_id, q, conversation_id, result, settings, route, cid, started)

    def _search_context(self, tenant, settings: dict):
        """What answering needs for one business: its search, the similarity function, and the thresholds."""
        search = self._search_for(tenant, settings)

        def sim(part: str, texts: list[str]) -> list[float]:
            return (self.embedder.embed_docs_cached(texts) @ self.embedder.embed([part], "query")[0]).tolist() if texts else []

        return search, sim, float(settings["thresholds"].get("floor", DEFAULT_FLOOR)), float(settings["thresholds"].get("evidence_sim_min", evidence.SEM_MIN))

    # ------------------------------------------------------------------ operations
    def ready(self) -> dict:
        """Can this instance serve? Booleans only: no address, no error text."""
        checks = {"database": False, "schema": False, "embedding_model": bool(getattr(self.embedder, "dim", 0))}
        try:
            checks["database"] = self.store._one("select 1")[0] == 1
            checks["schema"] = self.store._one("select to_regclass('public.ai_inference_settings') is not null")[0] is True
        except Exception:  # noqa: BLE001 - not ready is the answer; the reason stays in the log
            log.warning("readiness check failed", exc_info=False)
        return {"ready": all(checks.values()), "checks": checks}

    def alerts_get(self) -> dict:
        found = alerts.evaluate(self.tele, self.breakers.states())
        return {"firing": [a for a in found if a["firing"]], "rules": found}

    # ---- reading sources
    def ingest(self, business_id: str, user_id: str, source_id: str) -> dict:
        self._require(user_id, business_id, WRITE)
        if not is_uuid(source_id):
            raise ServiceError(400, "source_id must be an id")
        source = self.store.source(business_id, source_id)
        if source is None:
            raise ServiceError(404, "no such source")
        self.store.set_source_state(business_id, source_id, "processing", "pending", None)
        started = time.perf_counter()
        try:
            files = self._fetch(business_id, source)
            for name, _ in files:
                if SECRET_FILE.search(name):  # keys and passwords are not knowledge: they would be indexed, searched and quoted in answers
                    raise ServiceError(422, "this looks like a file of secrets (keys, passwords or certificates), not knowledge, so it was not read; delete it and keep secrets out of uploads")
            unscanned = self._scan(business_id, source_id, files)
            checksum = hashlib.sha256(b"".join(data for _, data in files)).hexdigest()
            if source["checksum"] == checksum and source["status"] == "ready" and source["active_rows"]:
                self.store.set_source_state(business_id, source_id, "ready", "parsed", None)
                return {"unchanged": True, "version": source["version"]}
            with tempfile.TemporaryDirectory() as tmp:
                paths = []
                for name, data in files:
                    p = Path(tmp) / Path(name).name
                    p.write_bytes(data)
                    paths.append(p)
                kb = load_kb(paths if len(paths) > 1 else paths[0])
            prefix = slug(source["name"])
            freshness = what_it_says_about_itself(kb)
            faqs, chunks, texts = [], [], []
            for i, e in enumerate(kb.entries):
                views = [(v.kind, v.text) for v in kb.views_of(i)]
                texts += [t for _, t in views]
                if e.kind == "faq":
                    faqs.append({"external_id": f"{prefix}:{e.id}", "question": e.question, "answer": e.answer, "category": e.category, "alt_questions": e.questions[1:], "views": views})
                else:
                    chunks.append({"ordinal": len(chunks), "heading": e.question, "content": e.answer, "hash": text_hash(e.answer), "views": views, "location": {"source": e.source, "section": e.question, **({"record": e.record} if e.record else {})},
                                   "flags": safety.injection_flags(f"{e.question}\n{e.answer}")})
            if len(texts) > MAX_VIEWS_PER_SOURCE:
                raise ValueError(f"this source makes {len(texts)} searchable pieces; the limit for one source is {MAX_VIEWS_PER_SOURCE}")
            vectors = self.embedder.embed(texts, "doc")
            k = 0
            for item in faqs + chunks:
                item["views"] = [(kind, text, vectors[k + j]) for j, (kind, text) in enumerate(item["views"])]
                k += len(item["views"])
            done = self.store.replace_content(business_id, source, faqs, chunks, self.model_name, checksum, "partial" if kb.warnings else "parsed", kb.warnings, freshness)
            self.store.record_usage(business_id, "ingest", latency_ms=int(1000 * (time.perf_counter() - started)))
            return {"unchanged": False, "faqs": len(faqs), "chunks": len(chunks), "flagged_passages": sum(bool(c["flags"]) for c in chunks), "scanned": not unscanned, **done}
        except (ValueError, webpage.WebError) as e:
            parse = "needs_browser" if "JavaScript" in str(e) else "failed"
            self.store.set_source_state(business_id, source_id, "failed", parse, str(e)[:500])
            raise ServiceError(422, str(e)[:500]) from None
        except ServiceError as e:
            self.store.set_source_state(business_id, source_id, "failed", "failed", e.message)
            raise
        except Exception:
            log.exception("ingesting source %s of business %s failed", source_id, business_id)
            self.store.set_source_state(business_id, source_id, "failed", "failed", "the source could not be processed")
            raise ServiceError(500, "the source could not be processed") from None

    def _scan(self, business_id: str, source_id: str, files: list[tuple[str, bytes]]) -> bool:
        """Scans each file before it is read. Returns True when nothing was scanned because no scanner is configured. A file that matches a signature is refused (and the match recorded, without the file);
        a file that could not be scanned is refused when scanning is required, and otherwise read with a note."""
        if isinstance(self.scanner, NullScanner):
            if self.scan_required:
                raise ServiceError(503, "uploads are required to be scanned and no scanner is configured")
            return True
        for name, data in files:
            try:
                self.scanner.scan(name, data)
            except Infected as e:
                self.tele.inc("upload_quarantined", tenant=business_id)
                self.store.record_audit(business_id, None, "source.quarantined", "knowledge_sources", source_id, {"signature": e.signature[:80]})
                raise ValueError("the file was refused: it matches a malware signature") from None
            except ScanError:
                self.tele.inc("scan_failed")
                if self.scan_required:
                    raise ServiceError(503, "the file could not be scanned, so it was not read; try again shortly") from None
                log.warning("a file of business %s could not be scanned; it was read unscanned (scanning is not required)", business_id)
                return True
        return False

    def purge_expired(self) -> dict:
        """Retention, enforced: each business's exchanges older than its own retention_days are deleted. Run by a scheduler through /v1/maintenance/purge."""
        out = self.store.purge_expired()
        telemetry.log_event(log, "purge", **out)
        return out

    def _fetch(self, business_id: str, source: dict) -> list[tuple[str, bytes]]:
        if source["type"] in ("web_page", "website"):
            if not source["url"]:
                raise ValueError("this source has no web address")
            pages, _ = webpage.read_site(source["url"], 1 if source["type"] == "web_page" else 10)  # refuses private addresses, obeys robots.txt
            return [(webpage.page_filename(p.url), webpage.page_html(p)) for p in pages]
        path = source["storage_path"]
        if not path or not path.startswith(f"{business_id}/"):
            raise ServiceError(400, "this source has no stored file in this business's folder")
        if self.fetch_object is None:
            raise ServiceError(503, "file storage is not configured on this service")
        data = self.fetch_object(path)
        return [(source["name"], data)]

    # ---- the model configuration
    def llm_public(self, business_id: str, user_id: str) -> dict:
        self._require(user_id, business_id, READ)
        cfg = self.store.llm_config(business_id) or {"provider": "", "model": "", "base_url": "", "enabled": False, "capabilities": {}, "has_key": False, "key_status": "unchecked"}
        conf = L.LLMConfig(enabled=cfg["enabled"], provider=cfg["provider"], model=cfg["model"], base_url=cfg["base_url"], api_key="x" if cfg["has_key"] else "")
        return {**conf.public(), "capabilities": cfg["capabilities"], "key_status": cfg["key_status"], "storage": "encrypted on the server (AES-256-GCM)", "available": self.secrets is not None,
                "in_use": self._in_use(business_id, cfg)}

    def _in_use(self, business_id: str, cfg: dict) -> dict:
        """Whether the model the business configured would actually be used for its next question, and if not, why. A model that is saved and silently unused would look like a model that works."""
        if not cfg["provider"] or not cfg["enabled"]:
            return {"used": False, "reason": "off"}
        r = self.router.route(business_id)
        if r.mode != "byok":
            return {"used": False, "reason": "mode_is_" + r.mode, "message": f"This business answers in {r.mode} mode, so the model saved here is not used. Choose \u201cYour own AI provider\u201d under Answer mode to use it."}
        if r.ok:
            return {"used": True, "reason": "agreed"}
        why = {"consent:provider_unverified": ("provider_unverified", "This provider's data-handling terms have not been verified by this service, so no data is sent to it and answers stay local."),
               "consent:missing_or_revoked": ("no_agreement", "The business has not agreed to send data to this provider, so no data is sent to it and answers stay local."),
               "config:ai_disabled": ("ai_disabled", "The service operator has not switched AI answering on, so answers stay local.")}.get(r.refusal or "", (str(r.refusal), "The model cannot be used right now (" + str(r.refusal) + "), so answers stay local."))
        return {"used": False, "reason": why[0], "message": why[1]}

    def _body_config(self, business_id: str, body: dict) -> tuple[dict, str | None]:
        provider = body.get("provider")
        if provider not in L.PROVIDERS:
            raise ServiceError(400, "choose a supported provider")
        spec = L.PROVIDERS[provider]
        model = body.get("model", "")
        base_url = body.get("base_url") or ""
        if not isinstance(model, str) or len(model) > 200 or not isinstance(base_url, str) or len(base_url) > 300:
            raise ServiceError(400, "model and base_url must be short text")
        if base_url and not spec["custom_url"]:
            base_url = ""  # a hosted deployment talks to the real provider address only
        if spec["custom_url"] and base_url and not base_url.startswith("https://") and not self.allow_private_llm:
            raise ServiceError(400, "the server address must start with https://")
        caps = body.get("capabilities") or {}
        if not isinstance(caps, dict) or any(not isinstance(v, bool) for v in caps.values()):
            raise ServiceError(400, "capabilities must be true/false values")
        key = body.get("api_key")
        if key is not None and (not isinstance(key, str) or not 8 <= len(key.strip()) <= 400):
            raise ServiceError(400, "that does not look like an API key")
        return {"provider": provider, "model": model.strip(), "base_url": base_url.strip(), "enabled": bool(body.get("enabled", False)),
                "capabilities": {"polish": True, "grounded": True, "assist": True, **caps}}, (key.strip() if key else None)

    def _stored_key(self, business_id: str) -> str:
        secret = self.store.llm_secret(business_id)
        if not secret or self.secrets is None:
            return ""
        try:
            return self.secrets.decrypt(secret[0], business_id, secret[1])
        except SecretError:
            return ""

    def llm_validate(self, business_id: str, user_id: str, body: dict) -> dict:
        self._require(user_id, business_id, ADMIN)
        fields, key = self._body_config(business_id, body)
        previous = self.store.llm_config(business_id)
        key = key or (self._stored_key(business_id) if previous and previous["provider"] == fields["provider"] else "")
        result = L.validate(L.LLMConfig(enabled=True, provider=fields["provider"], model=fields["model"], api_key=key, base_url=fields["base_url"]), allow_private=self.allow_private_llm)
        return {k: result[k] for k in ("ok", "code", "message", "models", "checked")}  # nothing of the configuration comes back

    def llm_save(self, business_id: str, user_id: str, body: dict) -> dict:
        self._require(user_id, business_id, ADMIN)
        fields, key = self._body_config(business_id, body)
        previous = self.store.llm_config(business_id)
        encrypted, status = None, None
        if key:
            if self.secrets is None:
                raise ServiceError(503, "this service has no credential key configured, so it cannot store an API key")
            encrypted = self.secrets.encrypt(key, business_id)
        elif previous and previous["provider"] != fields["provider"]:
            self.store.drop_llm(business_id, user_id)  # a key is never carried over to another provider
            previous = None
        if fields["enabled"]:
            have_key = bool(key) or bool(previous and previous["has_key"])
            if L.PROVIDERS[fields["provider"]]["key"] and not have_key:
                raise ServiceError(400, "enter the provider's API key before turning it on (or continue in local-only mode)")
            if not fields["model"]:
                raise ServiceError(400, "choose a model before turning it on")
            check = L.validate(L.LLMConfig(enabled=True, provider=fields["provider"], model=fields["model"], api_key=key or self._stored_key(business_id), base_url=fields["base_url"]),
                               allow_private=self.allow_private_llm)
            status = "valid" if check["ok"] else "invalid" if check["code"] in ("invalid_key", "forbidden") else None
            if not check["ok"]:
                raise ServiceError(422, check["message"])
        self.store.save_llm(business_id, user_id, fields, encrypted, status)
        return self.llm_public(business_id, user_id)

    def llm_remove(self, business_id: str, user_id: str) -> dict:
        self._require(user_id, business_id, ADMIN)
        self.store.drop_llm(business_id, user_id)
        return self.llm_public(business_id, user_id)


# ---- HTTP
def make_handler(engine: HostedEngine, shared_secret: str):
    routes = {"/v1/respond": lambda b: engine.respond(b.get("business_id"), b.get("user_id"), str(b.get("query") or ""), b.get("conversation_id"), b["_correlation_id"]),
              "/v1/sources/ingest": lambda b: engine.ingest(b.get("business_id"), b.get("user_id"), b.get("source_id")),
              "/v1/llm/get": lambda b: engine.llm_public(b.get("business_id"), b.get("user_id")),
              "/v1/llm/validate": lambda b: engine.llm_validate(b.get("business_id"), b.get("user_id"), b),
              "/v1/llm/save": lambda b: engine.llm_save(b.get("business_id"), b.get("user_id"), b),
              "/v1/llm/remove": lambda b: engine.llm_remove(b.get("business_id"), b.get("user_id")),
              "/v1/ai/disclosure": lambda b: engine.disclosure_for(b.get("business_id"), b.get("user_id"), b.get("provider")),
              "/v1/ai/consent/get": lambda b: engine.consent_get(b.get("business_id"), b.get("user_id")),
              "/v1/ai/consent/grant": lambda b: engine.consent_grant(b.get("business_id"), b.get("user_id"), b.get("provider"), b.get("disclosure_version")),
              "/v1/ai/consent/revoke": lambda b: engine.consent_revoke(b.get("business_id"), b.get("user_id"), b.get("provider")),
              "/v1/ai/settings/get": lambda b: engine.ai_settings_get(b.get("business_id"), b.get("user_id")),
              "/v1/ai/settings/save": lambda b: engine.ai_settings_save(b.get("business_id"), b.get("user_id"), b),
              "/v1/usage/get": lambda b: engine.usage_get(b.get("business_id"), b.get("user_id")),
              "/v1/history": lambda b: engine.history_get(b.get("business_id"), b.get("user_id"), b.get("limit", 50), b.get("before")),
              "/v1/local/prepare": lambda b: engine.local_prepare(b.get("business_id"), b.get("user_id"), str(b.get("query") or ""), b.get("conversation_id"), b["_correlation_id"]),
              "/v1/local/complete": lambda b: engine.local_complete(b.get("business_id"), b.get("user_id"), b.get("request_id"), b.get("text"), b.get("failure"), b.get("tokens_in", 0), b.get("tokens_out", 0)),
              "/v1/local/companions/get": lambda b: engine.companions_get(b.get("business_id"), b.get("user_id")),
              "/v1/local/companions/register": lambda b: engine.companion_register(b.get("business_id"), b.get("user_id"), b),
              "/v1/local/companions/revoke": lambda b: engine.companion_revoke(b.get("business_id"), b.get("user_id"), b.get("companion_id")),
              "/v1/feedback/approve": lambda b: engine.feedback_approve(b.get("business_id"), b.get("user_id"), b.get("faq_id"), b.get("question")),
              "/v1/feedback/learned": lambda b: engine.learned_list(b.get("business_id"), b.get("user_id")),
              "/v1/feedback/forget": lambda b: engine.learned_remove(b.get("business_id"), b.get("user_id"), b.get("learned_id")),
              "/v1/maintenance/purge": lambda b: engine.purge_expired()}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict, extra: dict | None = None) -> None:
            data = json.dumps(body, default=lambda o: float(o) if isinstance(o, np.floating) else int(o) if isinstance(o, np.integer) else str(o)).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            cid = getattr(self, "correlation_id", None)
            if cid:
                self.send_header("X-Request-Id", cid)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return hmac.compare_digest(given.encode(), shared_secret.encode())

        def do_GET(self):
            if self.path == "/health":  # for the container's health check: open to anyone who can reach the port, so it says nothing about traffic
                return self._send(200, {"ok": True, "model": engine.model_name, "dim": engine.embedder.dim, "pipeline": {"mode": engine.pipeline_mode}})
            if self.path == "/ready":  # may this instance take traffic? booleans only
                r = engine.ready()
                return self._send(200 if r["ready"] else 503, r)
            if self.path in ("/v1/metrics", "/v1/alerts"):  # counts and times (no business, person or text) and what is firing: behind the shared secret like every other route
                if not self._authorised():
                    engine.tele.inc("security_denied")
                    return self._send(401, {"error": "unauthorised"})
                return self._send(200, engine.metrics() if self.path == "/v1/metrics" else engine.alerts_get())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            self.correlation_id = telemetry.clean_correlation(self.headers.get("X-Request-Id")) or telemetry.new_correlation_id()
            if not self._authorised():
                engine.tele.inc("security_denied")
                return self._send(401, {"error": "unauthorised"})
            handler = routes.get(self.path)
            if handler is None:
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self._send(413 if length > MAX_BODY else 400, {"error": "the body must be 1 byte to 1 MB of JSON"})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "the body is not a JSON object"})
            body["_correlation_id"] = self.correlation_id  # set here, from the header only: a body cannot choose it
            try:
                self._send(200, handler(body))
            except ServiceError as e:
                self._send(e.status, {"error": e.message}, {"Retry-After": str(e.retry_after)} if e.retry_after else None)
            except SecretError as e:
                engine.tele.inc("request_error")
                self._send(503, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - the person gets a plain message; the details stay in the server's log (never the request body)
                engine.tele.inc("request_error")
                telemetry.log_event(log, "request_failed", logging.ERROR, correlation_id=self.correlation_id, route=self.path, error=type(e).__name__)
                log.exception("request to %s failed", self.path)
                self._send(500, {"error": "something went wrong on the server"})

        def log_message(self, fmt, *args):
            log.info("%s %s", self.command, self.path)

    return Handler


def from_env(env=os.environ) -> tuple[HostedEngine, str]:
    missing = [k for k in ("DATABASE_URL", "ENGINE_SERVICE_SECRET", "EMBEDDING_MODEL_DIR") if not env.get(k)]
    if missing:
        raise SystemExit("set these environment variables first: " + ", ".join(missing))
    if len(env["ENGINE_SERVICE_SECRET"]) < 24:
        raise SystemExit("ENGINE_SERVICE_SECRET must be at least 24 characters (use a long random string)")
    secrets = SecretBox.from_env(env) if env.get("LLM_CREDENTIAL_KEYS") else None
    fetch = StorageClient(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"]) if env.get("SUPABASE_URL") and env.get("SUPABASE_SERVICE_ROLE_KEY") else None
    try:
        mode = shadow.mode_from(env.get("ENGINE_PIPELINE"))
    except ValueError as e:
        raise SystemExit(f"ENGINE_PIPELINE: {e}") from None
    try:
        sample = float(env.get("ENGINE_SHADOW_SAMPLE", "1"))
    except ValueError:
        raise SystemExit("ENGINE_SHADOW_SAMPLE must be a number between 0 and 1") from None
    if not 0.0 <= sample <= 1.0:
        raise SystemExit("ENGINE_SHADOW_SAMPLE must be a number between 0 and 1")
    modes = {m.strip() for m in env.get("ENGINE_AI_MODES", "").split(",") if m.strip()}
    if modes - set(registry.AI_MODES):
        raise SystemExit(f"ENGINE_AI_MODES may only name {', '.join(registry.AI_MODES)} (got {', '.join(sorted(modes - set(registry.AI_MODES)))})")
    scanner = ClamdScanner(env["CLAMAV_HOST"], int(env.get("CLAMAV_PORT", "3310"))) if env.get("ENGINE_SCANNER") == "clamav" and env.get("CLAMAV_HOST") else NullScanner()
    if env.get("ENGINE_FAST_PATH", "on") not in ("on", "off"):
        raise SystemExit("ENGINE_FAST_PATH must be on or off")
    fast_path = env.get("ENGINE_FAST_PATH", "on") == "on"
    if env.get("ENGINE_SCANNER") == "clamav" and not env.get("CLAMAV_HOST"):
        raise SystemExit("ENGINE_SCANNER=clamav needs CLAMAV_HOST")
    telemetry.configure_logging(env.get("ENGINE_LOG_LEVEL", "INFO"), env.get("ENGINE_LOG_FORMAT", "json") == "json")
    engine = HostedEngine(Store(env["DATABASE_URL"]), Embedder(env["EMBEDDING_MODEL_DIR"]), secrets, fetch, allow_private_llm=env.get("ENGINE_ALLOW_PRIVATE_LLM") == "1",
                          rate_per_minute=int(env.get("RATE_LIMIT_PER_MINUTE", "60")), pipeline_mode=mode, shadow_sample=sample, ai_modes=frozenset(modes), platform=registry.PlatformCredentials.from_env(env),
                          inference_per_minute=int(env.get("ENGINE_INFERENCE_PER_MINUTE", "20")), max_platform_concurrent=int(env.get("ENGINE_PLATFORM_CONCURRENCY", "8")),
                          scanner=scanner, scan_required=env.get("ENGINE_SCAN_REQUIRED") == "1", fast_path=fast_path)
    return engine, env["ENGINE_SERVICE_SECRET"]


def serve(host: str = "0.0.0.0", port: int | None = None) -> None:  # noqa: S104 - a container listens on all addresses; the shared secret guards it, and so should the network
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    engine, secret = from_env()
    port = port or int(os.environ.get("PORT", "8090"))
    print(f"faqengine hosted service on {host}:{port} (model {engine.model_name}, credential storage {'on' if engine.secrets else 'off'}, reasoning pipeline {engine.pipeline_mode}, AI modes {sorted(engine.ai_modes) or 'none'}, file storage {'on' if engine.fetch_object else 'off'})", flush=True)
    ThreadingHTTPServer((host, port), make_handler(engine, secret)).serve_forever()


if __name__ == "__main__":
    serve()
