"""THE ORCHESTRATOR of the model-independent reasoning pipeline. One question in, one verified result out, every stage a function with a contract (contracts.py):

    understand -> retrieve (the existing hybrid search, unchanged) -> normalise -> validate -> [structured reasoning, composition: the engine's own, answer.respond]
               -> sufficiency -> [one targeted retrieval -> normalise -> validate -> compose again] -> compose (a Composer) -> verify every claim -> resolve -> result

Execution modes: deterministic (no model, nothing leaves the process), platform, byok, local (a provider behind contracts.Provider; none is implemented here). A model is only reached when the mode
asks for one, a provider is configured AND the business's consent allows that provider right now (checked here, on the server side, before every call; a revoked consent stops the next call).
A provider that fails (timeout, rate limit, server error) and a model whose answer cannot be used (invalid JSON, a claim the evidence does not support) are different failures and are recorded
as different; either falls back to the deterministic composition ONLY if the business allows it, and the result then says so (`execution.generated_by`): a fallback is never presented as a
model's verified answer. Nothing is logged but counts, ids and timings: no document text, no credentials. Standard library only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

try:
    from . import answer as A
    from . import claimcheck, compose, evidence_check, sufficiency, targeted, understand
    from . import evidence as ev
    from .contracts import Composition, EvidenceCheck, ExecutionMeta
except ImportError:  # an exported package puts runtime/ on the path
    import answer as A
    import claimcheck
    import compose
    import evidence as ev
    import evidence_check
    import sufficiency
    import targeted
    import understand
    from contracts import Composition, EvidenceCheck, ExecutionMeta

MODES = ("deterministic", "platform", "byok", "local")
OUTCOME = {"answered": "grounded", "partial": "partial", "conflict": "conflict", "insufficient": "insufficient"}


@dataclass
class FastPathDecision:
    taken: bool
    reason: str  # taken: exact_faq_direct | structured_computed; not taken: declined:<why> | not_eligible:<outcome>/<class>
    comp: Composition | None = None  # the deterministic composition that passed claim and citation verification (only when taken)


class _Search:
    """The question's retrieval, made once and shared: the engine's own composition and the pipeline's stages see the same candidates. Rejected entries are dropped (another business's, not
    current); extra hits found by the targeted pass are added to the passages of the first search."""

    def __init__(self, search, drop: set[str] | None = None, extra: list[dict] | None = None):
        self.search, self.drop, self.extra, self.model = search, drop or set(), extra or [], getattr(search, "model", None)
        self.cache: dict = {}
        self.question = ""

    def raw(self, q: str, on=None):
        key = (q, on)
        if key not in self.cache:
            self.cache[key] = self.search(q, on=on) if (on is not None and A._takes_day(self.search)) else self.search(q)
        return self.cache[key]

    def __call__(self, q: str, on=None):
        faq, passages, diag = self.raw(q, on)
        faq = [h for h in faq if h.get("entry_id") not in self.drop]
        passages = [h for h in passages if h.get("entry_id") not in self.drop]
        if self.extra and q == getattr(self, "question", q):
            have = {h.get("entry_id") for h in [*faq, *passages]}
            passages = passages + [h for h in self.extra if h.get("entry_id") not in have and h.get("entry_id") not in self.drop]
        return faq, passages, diag


class State:
    """Everything a request has established by the time it is composed. Held by the caller between `prepare` and `conclude_with_reply` when composition happens elsewhere."""

    def __init__(self, meta: ExecutionMeta, started: float):
        self.meta, self.t0, self._last = meta, started, started
        self.question = ""
        self.plan = self.evidence = self.checks = self.suff = self.base = self.ids = self.by_id = self.accepted = self.usable = self.det = self.sim = None

    def tick(self, name: str) -> None:
        now = time.perf_counter()
        self.meta.stage_ms[name] = round((now - self._last) * 1000, 2)
        self._last = now

    def update(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class Pipeline:
    def __init__(self, mode: str = "deterministic", provider=None, consent=None, interpreter=None, composer=None, allow_fallback: bool = True, tenant_id: str | None = None, fast_path: bool = False):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode, self.provider, self.consent, self.interpreter, self.composer, self.allow_fallback, self.tenant_id = mode, provider, consent, interpreter, composer, allow_fallback, tenant_id
        self.fast_path = fast_path  # the conservative deterministic fast path (evaluate_fast_path); off unless the caller opts in: the hosted service does

    # ------------------------------------------------------------------ the run
    def run(self, question: str, search, sim=None, as_of: date | None = None, similarity_floor: float = 0.4, sem_min: float = ev.SEM_MIN) -> dict:
        st = self.prepare(question, search, sim, as_of, similarity_floor, sem_min)
        if self.fast_path and self.mode != "deterministic" and self.composer is None:  # (a test double's composer is left alone; a deterministic business has no model to skip)
            decision = self.evaluate_fast_path(st)
            if decision.taken:
                return self.conclude_fast(st, decision)
        comp, composer_used = self._compose(st.plan, st.usable, st.suff, st.base, st.det, st.meta)
        st.tick("compose")
        return self._conclude(st, comp, composer_used)

    def prepare(self, question: str, search, sim=None, as_of: date | None = None, similarity_floor: float = 0.4, sem_min: float = ev.SEM_MIN) -> State:
        """Every stage BEFORE composition: understanding, retrieval, evidence with its immutable ids, validation, structured reasoning, sufficiency and the one targeted retrieval. Whoever then
        composes (the deterministic composer, a provider, or a model on the business's own machine) sees exactly this evidence, and `_conclude` verifies whatever they return."""
        meta = ExecutionMeta(mode=self.mode, provider=getattr(self.provider, "name", None), model=getattr(self.provider, "model", None))
        st = State(meta, time.perf_counter())
        today = as_of or date.today()
        shared = _Search(search)
        shared.question = A.normalize_query(question)
        model = (getattr(search, "model", None) or (lambda: None))()
        plan = understand.understand(question, set(model.by_entity) if model else frozenset(), self.interpreter, today)
        st.tick("understand")
        day = date.fromisoformat(plan.temporal["day"]) if plan.temporal.get("day") else today
        faq, passages, _ = shared(shared.question, day if plan.temporal.get("day") else None)
        evidence = evidence_check.normalize([*faq, *passages], self.tenant_id)
        st.tick("retrieve_normalize")
        checks = evidence_check.validate(plan, evidence, self.tenant_id, day, sim)
        hard = {e.entry_id for e, c in zip(evidence, checks, strict=True) if c.status == "rejected" and any(r.startswith(("cross_tenant", "expired", "superseded", "not_yet_valid", "not_current")) for r in c.reasons)}
        st.tick("validate")

        shared.drop = hard
        base = A.respond(question, shared, sim, None, similarity_floor, sem_min, as_of=today)
        st.tick("engine_compose")
        ids = {e.entry_id: e.id for e in evidence}
        evidence, checks = self._register_rows(base, evidence, checks, ids, plan, day, sim)
        suff = self._sufficiency(plan, evidence, checks, base)
        st.tick("sufficiency")

        if suff.needs_retrieval:  # at most one more pass, for a gap a search could close
            asked = {" ".join(shared.question.lower().split())}
            new, queries = targeted.second_pass(suff.gap_queries, asked, shared, {e.entry_id for e in evidence})
            meta.retrieval_passes = 2
            if new:
                more = evidence_check.normalize(new, self.tenant_id, start=len(evidence) + 1, known=ids)
                more_checks = evidence_check.validate(plan, more, self.tenant_id, day, sim)
                evidence, checks = evidence + more, checks + more_checks
                ids.update({e.entry_id: e.id for e in more})
                shared.extra = [h for h, e in zip(new, more, strict=True) if next(c for c in more_checks if c.evidence_id == e.id).status != "rejected"]
                shared.drop = shared.drop | {e.entry_id for e, c in zip(more, more_checks, strict=True) if c.status == "rejected"}
                base = A.respond(question, shared, sim, None, similarity_floor, sem_min, as_of=today)
                evidence, checks = self._register_rows(base, evidence, checks, ids, plan, day, sim)
            suff = self._sufficiency(plan, evidence, checks, base, second_pass_done=True)
            st.tick("targeted_retrieval")

        by_id = {e.id: e for e in evidence}
        engine_cited = [s for s in base.get("sources") or []] + [x for cf in base.get("conflicts") or [] for x in cf.get("sources", [])]  # what the engine's own assessment found to state it (and what it found in conflict)
        accepted = {c.evidence_id for c in checks if c.status == "accepted"} | {ids[s["id"]] for s in engine_cited if s.get("id") in ids and s["id"] not in hard}
        st.update(question=question, plan=plan, evidence=evidence, checks=checks, suff=suff, base=base, ids=ids, by_id=by_id, accepted=accepted, usable=[e for e in evidence if e.id in accepted],
                  det=compose.DeterministicComposer(ids), sim=sim)
        return st

    # ------------------------------------------------------------------ the fast path
    def evaluate_fast_path(self, st: State) -> FastPathDecision:
        """THE CONSERVATIVE DETERMINISTIC FAST PATH: may the model be skipped because the engine already has a complete, verified, evidence-backed answer? It reuses the stages that ran in `prepare`
        and adds nothing of its own to retrieval or reasoning; it only REFUSES to skip. Skipped only when ALL of this holds:

          * the engine's own result is an exact FAQ answer (`faq`, evidence class `direct`) or a fully supported structured one (`grounded`, class `computed`: a calculation, a lookup, a count or a
            comparison worked out in code). A match by meaning (`semantic`), a passage, a partial answer, a conflict or a clarification is never skipped past: it continues as before;
          * the question was understood and nothing is unresolved: no clarification is needed, no accepted sources disagree, no part is left unanswered;
          * sufficiency, assessed again WITHOUT the engine's own override, is SUFFICIENT: every requirement of the plan (every part of a multi-part question) is established by accepted evidence,
            nothing is unsupported or unverified, nothing is left for a second retrieval;
          * every piece of evidence the answer cites was accepted by validation, is current (not expired, not yet valid, not replaced), and belongs to this tenant;
          * the deterministic composition has at least one claim, every claim cites evidence, and it passes claim and citation verification (the same `claimcheck` every answer passes).

        A similarity score is never an input. Whatever is decided, the reason is recorded (`execution.fast_path_reason`). A decline changes nothing: the request continues through the pipeline exactly as it
        would have without a fast path."""
        meta, base, plan, suff = st.meta, st.base, st.plan, st.suff

        def decline(why: str) -> FastPathDecision:
            meta.fast_path_reason = f"declined:{why}"
            return FastPathDecision(False, meta.fast_path_reason)

        outcome, cls = base.get("outcome"), base.get("evidence_class")
        if outcome == "faq" and cls == "direct":
            kind = "exact_faq_direct"
        elif outcome == "grounded" and cls == "computed":
            kind = "structured_computed"
        else:
            meta.fast_path_reason = f"not_eligible:{outcome}/{cls}"
            return FastPathDecision(False, meta.fast_path_reason)
        if base.get("clarification"):
            return decline("needs_clarification")
        if base.get("conflicts") or suff.conflicts:
            return decline("unresolved_conflict")
        if base.get("unanswered_parts") or base.get("partial_answer"):
            return decline("part_unanswered")
        structured = [{"lines": True}] if cls == "computed" else []
        independent = sufficiency.assess(plan, st.evidence, st.checks, base.get("conflicts") or [], structured, base.get("clarification"), True)  # without the engine's override of the verdict
        if independent.status != "SUFFICIENT" or independent.unsupported or independent.unverified_conditions or independent.needs_retrieval or suff.needs_retrieval:
            return decline(f"sufficiency_not_confirmed:{independent.status}")
        requirements = plan.requirements or [plan.question]
        if not set(requirements) <= set(independent.supported) or len(plan.parts or []) > len(requirements):
            return decline("requirement_not_covered")
        cited = {st.ids[s["id"]] for s in base.get("sources") or [] if s.get("id") in st.ids}
        if not cited:
            return decline("no_cited_evidence")
        by_check = {c.evidence_id: c for c in st.checks}
        for eid in cited:
            c, e = by_check.get(eid), st.by_id[eid]
            if c is None or c.status != "accepted":
                return decline("evidence_not_accepted")
            if any(r.startswith(("expired", "superseded", "not_yet_valid", "not_current", "cross_tenant")) for r in c.reasons):
                return decline("evidence_not_current")
            if self.tenant_id is not None and e.tenant_id not in (None, self.tenant_id):
                return decline("evidence_of_another_tenant")
        comp = st.det.compose(plan, st.usable, suff, base)
        report = claimcheck.check(comp.claims, st.by_id, st.accepted, self.tenant_id, self._computed_numbers(base), plan.question, st.sim)
        if comp.verdict != "answered" or not comp.claims or not report.passed or any(not c.evidence_ids for c in comp.claims):
            return decline("deterministic_answer_not_verified")
        meta.fast_path_reason = kind
        return FastPathDecision(True, kind, comp)

    def conclude_fast(self, st: State, decision: FastPathDecision) -> dict:
        """Finish a request on the fast path: the deterministic composition that already passed verification, verified again in `_conclude` like every answer. No provider is reached and no
        consent is consulted, because nothing is sent anywhere."""
        st.meta.fast_path_taken = True
        st.tick("compose")
        return self._conclude(st, decision.comp, "deterministic")

    @staticmethod
    def _computed_numbers(base: dict) -> set[str]:
        return {n.replace(",", "") for r in (base.get("computed") or []) for n in [*r.get("numbers", []), *claimcheck.NUM.findall(str(r.get("values", "")))]}

    def conclude_deterministic(self, st: State) -> dict:
        """Finish a request that needs no model (a calculation done in code, or no usable evidence): the deterministic composition, verified like any other."""
        comp = st.det.compose(st.plan, st.usable, st.suff, st.base)
        st.tick("compose")
        return self._conclude(st, comp, "deterministic")

    def conclude_with_reply(self, st: State, reply) -> dict:
        """Finish a request whose composition was made elsewhere (a model on the business's own machine, reached by their browser): `reply` is what came back (contracts.ProviderReply, text and
        failure). It is as untrusted as any provider's: it is parsed, and every claim is verified against this request's own evidence before anything is returned."""
        meta = st.meta
        meta.inference_attempted = st.base.get("evidence_class") != "computed"
        composer = compose.ModelComposer(self.provider)
        composer.last_reply = reply
        if st.base.get("evidence_class") == "computed":  # arithmetic, counting, sorting and filtering are done in code: the model is not asked to restate them
            comp, used = st.det.compose(st.plan, st.usable, st.suff, st.base), "deterministic"
        else:
            comp = compose.from_reply(reply, st.plan, st.base, composer.name)
            if comp.failure:
                meta.failure_kind = comp.failure
                if self.allow_fallback:
                    meta.fallback = True
                    comp, used = st.det.compose(st.plan, st.usable, st.suff, st.base), "deterministic_fallback"
                else:
                    comp, used = Composition([], "insufficient", "", composer.name, st.base, comp.failure), "unavailable"
            else:
                used = "model"
        self._note_usage(meta, composer)
        st.tick("compose")
        return self._conclude(st, comp, used)

    def _conclude(self, st: State, comp: Composition, composer_used: str) -> dict:
        meta, plan, base, by_id, accepted, usable, det, suff = st.meta, st.plan, st.base, st.by_id, st.accepted, st.usable, st.det, st.suff
        sim = st.sim
        computed = self._computed_numbers(base)
        report = claimcheck.check(comp.claims, by_id, accepted, self.tenant_id, computed, plan.question, sim)
        if not report.passed and composer_used == "model" and self.allow_fallback:  # the model's answer did not hold: the deterministic one is verified in its place, and said to be
            meta.failure_kind = "reasoning:unsupported_claims"
            meta.fallback = True
            comp, composer_used = det.compose(plan, usable, suff, base), "deterministic_fallback"
            report = claimcheck.check(comp.claims, by_id, accepted, self.tenant_id, computed, plan.question, sim)
        final = claimcheck.resolve(comp, report, by_id)
        st.tick("verify")
        meta.generated_by = composer_used
        meta.answer_path = ("fast_path" if meta.fast_path_taken else {"model": "model", "deterministic_fallback": "deterministic_fallback", "deterministic_no_consent": "deterministic_no_consent", "unavailable": "unavailable",
                                                                     "mock": "mock"}.get(composer_used) or ("abstained" if comp.verdict == "insufficient" else "deterministic"))
        if not meta.fast_path_reason:
            meta.fast_path_reason = "not_applicable:deterministic_mode" if self.mode == "deterministic" else "disabled" if not self.fast_path else "not_evaluated"
        meta.validation = ("not_verified" if composer_used == "unavailable" else "verified" if report.passed and (comp.claims or comp.verdict == "insufficient") else "partly_verified" if final.claims else "not_verified")
        meta.inference_succeeded = composer_used == "model"
        meta.stage_ms["total"] = round((time.perf_counter() - st.t0) * 1000, 2)
        return self._result(base, final, report, plan, st.evidence, st.checks, suff, by_id, meta, comp is not final or composer_used == "model")

    # ------------------------------------------------------------------ pieces
    def _register_rows(self, base, evidence, checks, ids, plan, day, sim):
        """Rows of a table that structured reasoning used and retrieval had not returned get ids now, before any model composes anything."""
        rows = [s for s in base.get("sources") or [] if s.get("id") and s["id"] not in ids and s.get("kind") != "faq"]
        if not rows:
            return evidence, checks
        hits = [{"entry_id": s["id"], "question": (s.get("location") or "").split("› ")[-1], "answer": s.get("text", ""), "kind": "passage", "source": s.get("source"), "found_by": "structured",
                 "record": {"table": None}} for s in rows]
        more = evidence_check.normalize(hits, self.tenant_id, start=len(evidence) + 1, known=ids)
        ids.update({e.entry_id: e.id for e in more})
        return evidence + more, checks + [EvidenceCheck(e.id, "accepted", ["input row used by a structured operation"]) for e in more]

    def _sufficiency(self, plan, evidence, checks, base, second_pass_done: bool = False):
        structured = [{"lines": True}] if base.get("evidence_class") == "computed" and base.get("outcome") in ("grounded", "partial") else []
        clarification = base.get("clarification")
        conflicts = base.get("conflicts") or []
        s = sufficiency.assess(plan, evidence, checks, conflicts, structured, clarification, second_pass_done)
        if base.get("outcome") in ("faq", "grounded") and s.status in ("INSUFFICIENT", "PARTIAL") and not conflicts:
            s.status, s.action, s.unsupported, s.needs_retrieval, s.gap_queries = "SUFFICIENT", "answer", [], False, []  # the engine's own assessment established every part of the question
            s.notes.append("established by the engine's own evidence assessment")
        return s

    @staticmethod
    def _note_usage(meta: ExecutionMeta, composer) -> None:
        """What the one provider call used (tokens, estimated cost, time, attempts), for metering. Nothing of its text."""
        r = getattr(composer, "last_reply", None)
        if r is not None and meta.inference_attempted:
            meta.tokens_in, meta.tokens_out, meta.cost = int(r.tokens_in or 0), int(r.tokens_out or 0), r.cost
            meta.provider_ms, meta.provider_attempts = round((r.seconds or 0.0) * 1000, 2), int(getattr(r, "attempts", 1) or 0)

    def _consent_ok(self, meta: ExecutionMeta) -> bool:
        if self.mode == "deterministic":
            return False
        if self.provider is None:
            meta.failure_kind = "consent:no_provider"
            return False
        refusal = getattr(self.provider, "refusal", None)  # a route the router could not give (a limit, a circuit that is open, no companion): said as it was, and nothing is sent
        if refusal:
            meta.failure_kind = refusal
            return False
        if self.consent is None or not self.consent.allows(self.tenant_id, self.provider.name):  # server side, every call: revoking takes effect on the next question
            meta.failure_kind = "consent:missing_or_revoked"
            return False
        return True

    def _compose(self, plan, usable, suff, base, det, meta):
        if self.composer is not None and getattr(self.composer, "name", "") == "mock":  # a test double: it is labelled as one and never reaches a provider
            return self.composer.compose(plan, usable, suff, base), "mock"
        if not self._consent_ok(meta):
            if meta.failure_kind and meta.failure_kind.startswith("consent"):
                return det.compose(plan, usable, suff, base), "deterministic_no_consent"  # nothing was sent, and nothing was tried
            if meta.failure_kind and meta.mode != "deterministic":  # the chosen AI could not run (limit, open circuit, no companion): the answer says so, and is the deterministic one only if the business allows
                if self.allow_fallback:
                    meta.fallback = True
                    return det.compose(plan, usable, suff, base), "deterministic_fallback"
                return Composition([], "insufficient", "", "unavailable", base, meta.failure_kind), "unavailable"
            return det.compose(plan, usable, suff, base), "deterministic"
        if not usable:  # nothing to compose from: a model cannot add what the evidence does not hold, so none is asked (and none is paid for)
            return det.compose(plan, usable, suff, base), "deterministic"
        composer = self.composer or compose.ModelComposer(self.provider)
        meta.inference_attempted = base.get("evidence_class") != "computed"
        comp = composer.compose(plan, usable, suff, base)
        self._note_usage(meta, composer)
        if comp.failure:
            meta.failure_kind = comp.failure
            if self.allow_fallback:
                meta.fallback = True
                return det.compose(plan, usable, suff, base), "deterministic_fallback"
            return Composition([], "insufficient", "", composer.name, base, comp.failure), "unavailable"
        return comp, "model" if composer.name != "deterministic" and meta.inference_attempted else "deterministic"

    def _result(self, base, final: Composition, report, plan, evidence, checks, suff, by_id, meta, replaced: bool) -> dict:
        out = dict(base)
        if replaced or report.removed or final.composer == "unavailable" or meta.generated_by == "unavailable":
            ids = list(dict.fromkeys(i for c in final.claims for i in c.evidence_ids if i in by_id))
            number = {i: n for n, i in enumerate(ids, 1)}
            out["sources"] = [{"ref": number[i], "kind": by_id[i].source_type if by_id[i].source_type == "faq" else "passage", "id": by_id[i].entry_id, "source": by_id[i].source_name, "location": by_id[i].location,
                               "text": by_id[i].content[:300]} for i in ids]
            outcome = OUTCOME.get(final.verdict, "insufficient")
            out.update(outcome=outcome, answer=final.answer if outcome == "grounded" else None, partial_answer=final.answer if outcome == "partial" else None,
                       message=("AI inference is temporarily unavailable." if meta.generated_by == "unavailable" else out.get("message") if outcome in ("insufficient", "conflict") else None),
                       claims=[{"text": c.text, "refs": [number[i] for i in c.evidence_ids if i in number], "evidence_ids": c.evidence_ids, "kind": c.kind} for c in final.claims])
            if outcome == "insufficient":
                out["answer"] = None
                out["message"] = out.get("message") or "The information I have does not answer this."
        if not replaced:  # the engine's own claims, in the pipeline's shape: which evidence ids each rests on, and what kind of statement it is
            old = out.get("claims") or []
            out["claims"] = [{**(old[i] if i < len(old) and len(old) == len(final.claims) else {"text": c.text, "refs": []}), "evidence_ids": c.evidence_ids, "kind": c.kind} for i, c in enumerate(final.claims)]
        out["query_plan"] = {k: getattr(plan, k) for k in ("intent", "entities", "attributes", "comparison", "scope", "qualifiers", "temporal", "output_format", "needs_structured", "multi_source", "parts",
                                                             "requirements", "interpreted_by")} | {"conditions": [{"text": c.text, "kind": c.kind, "claim_type": c.claim_type, "qualifier": c.qualifier} for c in plan.conditions]}
        out["evidence"] = [{"id": e.id, "entry_id": e.entry_id, "tenant": e.tenant_id, "source": e.source_name, "source_type": e.source_type, "location": e.location, "status": c.status, "reasons": c.reasons,
                            "supports": sorted(c.supports)} for e, c in zip(evidence, checks, strict=True)]
        out["sufficiency"] = {"status": suff.status, "supported": suff.supported, "unsupported": suff.unsupported, "unverified_conditions": suff.unverified_conditions, "conflicts": len(suff.conflicts),
                              "needs_retrieval": suff.needs_retrieval, "gap_queries": suff.gap_queries, "action": suff.action, "notes": suff.notes}
        out["claim_verification"] = {"passed": report.passed, "removed": report.removed, "claims": report.claims, "issues": [{k: v for k, v in i.items() if k != "claim"} for i in report.issues]}
        out["execution"] = {"mode": meta.mode, "provider": meta.provider, "model": meta.model, "inference_attempted": meta.inference_attempted, "inference_succeeded": meta.inference_succeeded,
                            "fallback": meta.fallback, "generated_by": meta.generated_by, "failure_kind": meta.failure_kind, "validation": meta.validation, "retrieval_passes": meta.retrieval_passes,
                            "stage_ms": meta.stage_ms, "usage": {"tokens_in": meta.tokens_in, "tokens_out": meta.tokens_out, "cost": meta.cost, "provider_ms": meta.provider_ms, "attempts": meta.provider_attempts},
                            # how this answer was made, said once: the path, why a model was or was not skipped, whether a model was invoked, which providers were tried, what it rests on, how it verified, how long it took
                            "answer_path": meta.answer_path, "fast_path_reason": meta.fast_path_reason, "model_invoked": bool(meta.inference_attempted),
                            "provider_steps_tried": [meta.mode] if meta.inference_attempted and meta.mode != "deterministic" else [],
                            "evidence_ids": list(dict.fromkeys(i for c in final.claims for i in c.evidence_ids)),
                            "verification": {"outcome": meta.validation, "passed": report.passed, "claims_checked": len(report.claims), "claims_removed": report.removed},
                            "latency_ms": meta.stage_ms.get("total")}
        out["verified"] = meta.validation == "verified"
        return out
