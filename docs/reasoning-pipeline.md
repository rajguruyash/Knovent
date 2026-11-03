# The model-independent reasoning pipeline

One question goes through eight stages, each a function with a contract (`runtime/contracts.py`) that can be tested alone. Nothing in the pipeline knows any provider or model: a provider sits behind
`contracts.Provider`, and **deterministic mode needs none**. It was built so that whichever provider wins the model benchmark can be put behind the same interfaces without changing the stages.

```
question ─► 1 understand ─► 2 retrieve (the existing hybrid search, unchanged) ─► 3 normalise ─► 4 validate ─► [structured reasoning + composition: the engine's own]
         ─► 5 sufficiency ─► 6 targeted retrieval (at most once) ─► 7 compose (a Composer) ─► 8 verify every claim ─► resolve ─► result + execution record
```

| Stage | Module | What it takes and returns | Model? |
|---|---|---|---|
| 1 Query understanding | `understand.py` | question → `QueryPlan`: intent, entities, attributes, conditions (each with kind, claim type, qualifier), comparison, scope, qualifiers, temporal constraint, output format, structured/multi-source flags, requirements | none (an optional `Interpreter` may *add* to a plan, never remove) |
| 2 Retrieval | `retrieval.py` (existing) | one hybrid ranking over every kind of source | none |
| 3 Normalisation | `evidence_check.normalize` | hits → `Evidence` with ids `E1…`, tenant, source, kind, location, heading, content, validity, how it was retrieved. **Ids are made here, before any model, and never change** (frozen objects; later items continue the numbering) | none |
| 4 Validation | `evidence_check.validate` | each item → `accepted` / `context` / `rejected` with reasons | none |
| 5 Sufficiency | `sufficiency.py` | plan + checks → `SUFFICIENT` / `PARTIAL` / `CONFLICTED` / `INSUFFICIENT` / `AMBIGUOUS`, with what is supported, what is not, which conditions are unverified, the gap queries and the action (answer / qualify / refuse) | none |
| 6 Targeted retrieval | `targeted.py` | gap queries → at most **one** extra pass of at most two queries, deduplicated, never a query already asked, no quota on any kind of source | none |
| 7 Composition | `compose.py` | `Composer.compose(plan, evidence, sufficiency, base) → Composition` (claims with evidence ids). `DeterministicComposer` (the engine's own answer as claims), `MockComposer` (tests), `ModelComposer` (a provider behind the interface) | only `ModelComposer` |
| 8 Claim verification | `claimcheck.py` | claims → per-claim `verified` / `removed` / `not_factual` with issues; `resolve` rebuilds the answer from what held | none |

The structured layer (`structured.py`: comparisons, minimum and maximum, filters, counts, sums, differences, price arithmetic, limit and eligibility checks, all over the rows of the business's own tables) is the engine's, reused unchanged except for what this phase added (below); **no arithmetic, counting, sorting or filtering is ever given to a model**: when a question is answered by it, `ModelComposer` does not call the provider at all, and the claims it produced are checked as calculations against the cited rows and the operation's own numbers.

## What validation rejects, and why a score is never support
Rejected: another business's evidence (`cross_tenant`), expired, not yet valid, replaced by a source in force (`validity`), and what nothing in it bears on (`irrelevant`). `context` (kept, never cited): a statement about the same thing that does not state what was asked ("annual billing is available" for "is there an annual discount?"; "we aim for 99.9%" for "do you guarantee 99.9%?"; "Team plan: 24 hours" for the Growth plan). Support is a sentence that states the requirement (`evidence.classify`, with its claim types, scopes and dates); the retrieval similarity is only the second sign the engine's own assessment asks for beside a sentence close in meaning, and never support alone.

## What claim verification checks
Every claim is checked against **only the evidence it cites**: the id exists, belongs to this business and was accepted; the cited evidence states it (wording, figures, dates, polarity, commitments, and the conditions the cited sentence carries: `verify.verify`); a claim of a kind (discount, refund, trial, guarantee) is stated as that kind; the scope is not widened ("all plans", "every Enterprise deployment", "always"); a calculated figure is a figure of the cited rows, with its unit, or a result of the operation. A claim that fails is removed; the answer is rebuilt from the claims that held, marked partial and says that a statement was left out; if none held it is an abstention. An answer that lost a claim is never reported as verified (`verified: false`, `execution.validation`).

## Execution modes, consent and failure
`deterministic` (nothing leaves the process), `platform`, `byok`, `local` (the last three need a provider behind `Provider`; none is implemented here; no Cloudflare, OpenRouter or local companion code exists in the pipeline). A provider is reached only if a provider is configured **and** `ConsentPolicy.allows(tenant, provider)` is true at that moment, checked on the server before every call: revoking takes effect on the next question. Without consent the answer is the deterministic one and says so (`generated_by: deterministic_no_consent`).

| What went wrong | Recorded as | What happens |
|---|---|---|
| provider timeout, rate limit, server error, bad credentials | `failure_kind: provider:<kind>` | deterministic fallback if the business allows it (`fallback: true`, `generated_by: deterministic_fallback`), otherwise `outcome: insufficient`, "AI inference is temporarily unavailable", `generated_by: unavailable` |
| invalid JSON, wrong schema, malformed claim | `reasoning:invalid_json` / `schema` / `malformed_claim` | the same |
| claims the evidence does not carry | `reasoning:unsupported_claims` | fallback if allowed; otherwise the unsupported claims are removed and the rest is marked partial and unverified |

`execution` on every result: mode, provider, model, whether inference was attempted and succeeded, whether it fell back, who generated the answer (`deterministic`, `model`, `deterministic_fallback`, `deterministic_no_consent`, `mock`, `unavailable`), the failure kind, the validation outcome, retrieval passes and the time of each stage. Only counts, ids and timings are recorded: no document text and no credentials.

## What this phase changed outside the pipeline
* **Conjunctions are no longer half-answered.** "Which plans support both WhatsApp and SSO?" and "…more than 5 agents and include WhatsApp?" now return the things that have **all** the conditions (`structured.membership` intersection, `compound_filter`); before, only one condition was used. A sufficiency assessment treats conditions that hold separately as `PARTIAL` unless an operation established them together.
* Evidence-engine and structured-engine work of Phase G (claim types, dates inside sentences, vocabulary, conflicts, spans, tables as data) is described in `docs/unified-knowledge.md` and `docs/evidence-architecture.md`.

## Measured (diagnostics, deterministic mode; not independent benchmark results)
`python scripts/pipeline_report.py` (the locked and verify sets are refused). In deterministic mode the pipeline's outcome equals the engine's on every question of every set, and no claim of a deterministic answer was removed.

| Set (questions) | Correct | False answers | Citation correct | Unnecessary abstention | Partial correct | Conflicts found | Structured correct | Sufficiency P / R: SUFFICIENT · PARTIAL · CONFLICTED · INSUFFICIENT |
|---|---|---|---|---|---|---|---|---|
| Beacon dev (59), developed against | 100% | 0% | 100% | 0% | 100% | 100% | 33/33 | .92/1.0 · 1.0/1.0 · 1.0/1.0 · 1.0/.64 |
| Beacon regression (18) | 100% | 0% | 100% | 0% | 100% | 100% | 9/9 | .93/1.0 · 1.0/1.0 · 1.0/1.0 · 1.0/.50 |
| Acme dev (48), developed against | 100% | 0% | 100% | 0% | 100% | 100% | – | 1.0/1.0 · 1.0/.67 · 1.0/1.0 · .86/1.0 |
| Acme regression (9) | 100% | 0% | 100% | 0% | 100% | – | – | 1.0/1.0 · 1.0/1.0 · – · 1.0/1.0 |
| **Beacon fresh (20), not tuned on** | 70% | 20% | 82% | 7% | – | 33% | 5/5 | .78/1.0 · – · 1.0/.33 · 0.0/0.0 |
| **Acme fresh (20), not tuned on** | 75% | 19% | 88% | 0% | – | 50% | 3/4 | .88/.93 · 0/– · 1.0/.50 · .50/.33 |

Query-understanding accuracy 16/16 and evidence-validation accuracy 10/10 are on **labelled synthetic cases** (`tests/pipeline_cases.py`), written to exercise each rule; they are not a measurement of how well questions in general are understood. **No mock provider or mock composer result is reported as a model result.**

The fresh sets are the honest figures: the engine still answers missing-information and conflict questions it should decline or disclose (sufficiency recall for INSUFFICIENT 0.33 and 0.00; conflicts found 1 of 3 and 1 of 2). The pipeline's verification does not catch these because the evidence it is given really does contain a sentence that looks like support; they are the cases a reasoning model would be asked to improve.

## In the hosted service and the Studio (Phase H, 2026-11-02)
One setting, `ENGINE_PIPELINE` for the hosted service and `FAQENGINE_PIPELINE` for the Studio (`off` by default; a typo stops the process instead of meaning off):

| Setting | Who answers | What else happens |
|---|---|---|
| `off` | the existing path (`answer.respond`) | nothing; the response gains one `execution` line saying `path: legacy` |
| `shadow` | the existing path | the pipeline runs on the **same request**; `runtime/shadow.compare` lists differences; counted, and each mismatch is written to `audit_logs` (`pipeline.shadow_mismatch`: phrases and ids, never question or answer text). The response is not duplicated and does not change |
| `on` | the pipeline (deterministic) | `execution.path: pipeline`; a fault in the pipeline is counted, recorded by exception *type* only, and the existing path answers instead (`execution.pipeline_note`) |

Rollback is the setting and a restart; the existing path is untouched and is also the automatic fallback. A business whose own language model is on and agreed to keeps the existing path (labelled), because the pipeline's model composer is not connected to anything. **No external model is called by this product in any setting**: `HostedEngine.provider_factory` exists so tests can prove the consent checks with a mock provider, and nothing sets it. The Studio's pipeline is constructed as `Pipeline("deterministic")` with no provider and no consent policy; `ProjectEngine.respond` has no parameter that could add one.

What the hosted response now carries beyond the old contract (additive; nothing removed): `execution` (path, mode, provider, model, inference attempted/succeeded, fallback, `generated_by`, `failure_kind`, validation, `retrieval_passes`, `stage_ms`), and in `on` mode `query_plan`, `evidence` (id, entry id, tenant, source, judgement, reasons, which requirement it states; no document text), `sufficiency` (including the targeted-retrieval decision), `claim_verification` and `claims` with the evidence ids each rests on. Stored with the exchange in `messages.llm.execution` and `messages.evidence`. Studio shows the same in "How this was decided".

**Shadow parity was measured, not assumed** (real schema, real embedding model, stored passages and vectors; `tests/test_hosted_pipeline.py`): Acme dev + regression (57 questions) and Beacon dev + regression (77 questions) through the hosted service, plus expiry and replacement on a moving clock; the Studio on the evidence example (dev + regression). The first run found **8 of 57** Acme differences. Reading them: 6 were the pipeline's one targeted second retrieval *adding* candidates after the first search's (by design), and 2 were conflicts, where the pipeline lists each side's statement as claims and the existing result lists them only under `conflicts`. The comparator now allows exactly those two things, narrowly (extra candidates only at the end, in order, and only when `retrieval_passes` is 2; a conflict's sides read as claims on both paths; `tests/test_shadow.py` checks that reordering, dropping, a dropped side and an extra candidate without a second pass are still differences). Every outcome, answer, cited source, conflict count, abstention, structured result and freshness decision was identical before and after that change.

## Consent (migration 10, `docs/ai-disclosure.md`)
`ai_inference_consents`: one row per agreement, per business, provider and disclosure version, with who agreed and when, the exact text's SHA-256, the data categories and, if withdrawn, who and when. Only owners and admins can create a grant or revoke it; a grant can only be revoked (no edit, no un-revoke, no delete; the history stays); members of other businesses cannot see it; there is no credential column; every change is in the audit trail. The service asks the database before **every** external call (`DbConsent`, never cached) and the pipeline refuses when it says no: `generated_by: deterministic_no_consent`, `failure_kind: consent:missing_or_revoked`, nothing sent. Consent for one provider is not consent for another; a changed disclosure text is a new version and ends the earlier agreement. A business's own configured model is gated the same way, and its stored key is **not even decrypted** for a provider the business has not agreed to. Routes: `POST /v1/ai/disclosure|consent/get|consent/grant|consent/revoke` (read for any member, grant and revoke for owner/admin; grant must echo the current version).

## Phase I additions (2026-11-02)
* **Controlled shadow:** `ENGINE_SHADOW_SAMPLE` (0 to 1) chooses the share of requests the pipeline also runs on; every difference is named by kind (`answer`, `claims`, `citations`, `evidence`, `abstention`, `conflict`, `structured`, `tenant`; `runtime/shadow.categories`), counted, and written to `audit_logs` (at most 50 per business and hour; the rest are counted as `shadow_unrecorded`). `compare(..., strict=True)` switches off the two allowances above so that their use can be counted. `GET /v1/metrics` (behind the shared secret) reports the counters and the median and 95th percentile of both paths' times; `/health` stays open and reports only the mode.
* **What shadow costs:** measured on synthetic traffic, inline shadow roughly doubles the time of a sampled request on the test machine (`docs/shadow-validation.md`). The pipeline repeats the engine's own composition and adds the understand, validate, sufficiency and verify stages. Running it after the response is sent, in a bounded worker, would take that off the customer's wait; it has not been built.
* **Consent now needs a verified provider** (`docs/provider-verification.md`): a provider whose terms are not recorded is not offered, not agreed to and not used, checked on every call. A business that configured its own model for such a provider keeps local answers, and Settings says "Not in use" and why.
* **Migration 11** closes the privilege gaps the catalog audit found; `scripts/validate_supabase.py` checks the same things on a Supabase project (`docs/staging-validation.md`).

## Remaining gaps before a provider is attached
1. No production provider, router, circuit breaker or per-business provider selection. `provider_factory` is a test seam. `ModelComposer` has only been run against test doubles; its prompt and a model's JSON reliability are untested on any real model.
2. The Vercel functions (`web/api/ai/*`) and the Settings screen for reading the disclosure and agreeing are built and tested, but not deployed and not run against a live Supabase or the real service. All provider disclosure entries are empty: nobody has verified any provider's terms yet, so the screen offers nothing.
3. Migrations 10 and 11 have been applied only to the test PostgreSQL (version 18; Supabase runs 17), not to a live Supabase project; in particular the guard's treatment of the server connection (`current_user = 'postgres'`, `auth.role()`) is modelled by the stand-in. `scripts/validate_supabase.py` is ready for a staging project that does not exist yet.
4. `shadow` has run on example data, not on live traffic. Before `on` for customers, run `shadow` in the real service and read `pipeline.shadow_mismatch`; the rollout counters are in `GET /v1/metrics`.
5. A business with its own model turned on is not covered by the pipeline.
6. The deterministic query understanding is rule-based (16 labelled cases); sufficiency is only as good as the engine's own assessment (the fresh-set failures are the engine's, and the pipeline inherits them).
7. Stage `validate` costs about 50 ms per question on this machine, and `shadow` doubles the work of a request while it runs.

## Recommended next phase
Run `shadow` in the live service, then attach the provider(s) that the model benchmark supports (the benchmark still needs Cloudflare and OpenRouter credentials in the environment), and measure `ModelComposer` on the fresh sets with identical frozen evidence before enabling it for anyone.
