# Final report: the unified business knowledge engine, 2026-11-02

**Verdict: not production-ready, and not claimed to be.** Everything that can be built and tested without external accounts, credentials, a deployment or a human reading of providers' terms is built and tested. Each of the eight release gates still has an item that is BLOCKED or FAILED (table in section 17). Nothing is deployed; nothing is committed or pushed (as instructed); no real AI provider has been called; no benchmark result for any model exists.

The documents behind each section are named in it. Numbers come from runs recorded in `runs/` or from the test suites on the date above; where something was not measured, it says so.

## Update: the local-first correction (2026-11-02, later the same day)

The product's order of preference is **local AI first, free hosted second, paid hosted third, with the business's own provider account (BYOK) as an optional preferred or alternative provider**; the three execution modes are not equal options. The router, the settings, the setup experience, the companion and the documents were changed to say and enforce that, and everything outside the order was left as it was (a business that turns nothing on has exactly one step and behaves exactly as before). The new policy, its enforcement, the hardware rules, the status of model download and management, and what real-device testing remains are in **`docs/local-first.md`**; the gates are re-assessed in `docs/production-readiness.md` (Gate 3). In short: enabling AI means *your own computer, alone*; each outside step is off until the business turns it on **and** agrees to that provider's verified terms (refused at save, checked again at every request); free is tried before paid and paid stops at the business's own spending cap; a failed provider hands over only to a step the business enabled, while a bad answer or a Cancel hands over to nothing; every answer says which step made it and which were tried; the page sets up a local model with one confirmation (it names the model, its size and the free disk first); several small, fast models are offered; a `.env` file can be dropped to connect an API service (read in the browser for one key, never stored or uploaded) and is never accepted as knowledge. **One real model has now been run through the chain as a smoke test** (Ollama 0.40.13 installed with the owner's say-so, `llama3.2:1b` downloaded once, 1.23 GB; paired, downloaded, selected, registered, health, 3 real questions, cancelled in 0.01–0.05 s, only `127.0.0.1` contacted outside the download): `scripts/local_e2e_check.py`, results in `docs/local-first.md`. The 1B model's answers were not better than the deterministic ones on those questions, and nothing else about real hardware has been tested.

## Update: the deterministic fast path and the interface (2026-11-02, later still)

**A question the engine already answers completely, currently and verifiably no longer goes to a model.** An exact FAQ entry, or a structured answer worked out in code, is answered at once when every condition holds (understood; every part covered; accepted, current evidence of this business; no conflict; the deterministic answer passes claim and citation verification) and otherwise continues through the pipeline unchanged. No consent, fallback permission, model selection or customer setting changed; a similarity score is never an input; `ENGINE_FAST_PATH=off` restores the old behaviour. Every answer records `answer_path`, `fast_path_reason`, `model_invoked`, `provider_steps_tried`, evidence ids, verification and latency. **Measured against the deterministic baseline** on 174 development, regression and fresh questions (sealed sets untouched): 65 skipped the model, **64 correct (98.5%), citations 100%, all identical to the baseline's answer, median 31.6 ms (the decision costs 0.1 ms)**; against a real `llama3.2:1b` on the seven exact FAQ answers the fast path was 7 of 7 and the model path 5 of 7 (after verification rejected its bad answers) at a median 9.1 s. **Honest findings:** only 7 of the 65 are the new skip (58 were already code-computed), one wrong structured answer is inherited from the baseline and was not tuned, and a model's `answered` verdict still overrides the engine's partial/conflict (pre-existing, unchanged). Details: `docs/fast-path.md`.

**The web interface was redesigned** as an original dark "AI product" design (aurora hero, glass navigation, a how-it-works strip, a capabilities grid, the local-first chain, a comparison, FAQ). **The two Webflow templates named were not used:** their pages could not be opened here, and paid templates should not be copied exactly in any case, so the design only shares their genre. The redesign found and fixed a real defect: script-set inline styles were blocked by the production Content-Security-Policy; the page now uses the CSS object model and a test forbids inline styles. Checked in a browser at desktop and 375 px (no horizontal overflow); the preview server now sends the production security headers so this cannot regress unseen.

## 1. Final architecture

Browser → Vercel functions (authenticate, authorise, whitelist) → engine service (router → one reasoning pipeline → provider adapters) → Supabase Postgres with row-level security; local mode detours through the person's browser and a companion on their own computer. Diagram, trust boundaries and the pipeline stages: `docs/architecture.md`.

## 2. Implemented feature inventory

| Area | What exists (all tested unless marked) |
|---|---|
| Knowledge | one ingestion path for FAQ tables, prose tables, workbooks, nested JSON, Markdown/text/HTML/PDF/Word, web pages; freshness and supersession; one hybrid ranking; structured reasoning over table rows (comparisons, counts, sums, price arithmetic, limits) |
| Answering | deterministic, evidence-grounded, can say "I don't know"; conflicts surfaced; claims verified against immutable evidence ids; a lost claim means "not verified" |
| Execution modes | **local first** (Ollama, llama.cpp via a companion), then, only if the business turns them on and agrees to each provider: its own provider account (OpenAI, Anthropic, Gemini, OpenRouter, any OpenAI-compatible server), free hosted AI, paid hosted AI; deterministic by default and always available; all through the same pipeline |
| Routing and policy | an ordered plan (local → own account → free → paid), each step off unless turned on; operator kill switch, verified terms + per-provider/per-version consent, free quota, plan limits and the business's own spending cap, abuse limit, shared-capacity cap, circuit breakers, no step added by a failure, `fallback` deterministic or none |
| Security | RLS on every table, privilege hardening, encrypted write-only keys, SSRF pinning, no redirects, CORS/CSRF posture, signed loopback companion, upload scanning hook and budgets, retention purge, audit trail |
| Customer product | sign-up, businesses, invitations and roles, uploads with progress, re-read, removal, Test it with citations, History showing who generated each answer, provider settings, consent screen, mode selection, usage and plan, companion pairing, Studio |
| Operations | correlation ids, structured logs, `/health` `/ready` `/v1/metrics` `/v1/alerts`, 11 alert rules, runbooks |
| Evaluation | benchmark harness on the production adapters with the seventeen measures; frozen evidence; sealed 49-question holdout; performance script |
| Not built | billing/payment, invitation e-mail, job queue for big uploads, engine connection pooling, a deployed anything |

## 3. Three-mode inference architecture

`docs/architecture.md` (the order, router, adapters, pipeline), `docs/local-first.md` (the policy) and `docs/local-companion.md`. In one line each: **local** (first) keeps evidence on the business's machine and sends nothing to a provider; **free hosted** and **paid hosted** use the operator's credential for the model the operator released for that tier, only after the business turned the step on and agreed to that provider's verified text, within the free quota or the plan and the business's spending cap; **BYOK** uses the business's own encrypted key under the same consent rule, decrypted last. When no permitted step can answer, the result is the deterministic answer (labelled) or "unavailable"; evidence is never sent to a provider the business did not enable and agree to.

## 4. Provider and model comparison

`docs/model-selection.md`. Six providers/server types, six candidate cloud models and six local candidates, with prices as recorded from the providers' own pages on 2026-11-01 (to be re-read before use). **No provider's terms are verified (0 of 6) and no model is offered.** There is no measured comparison of models because none could be run.

## 5. Benchmark results

**None for any model: BLOCKED** (credentials `CLOUDFLARE_*` and `OPENROUTER_API_KEY` are MISSING in the shell; local models are too slow for this machine). What exists is the baseline the models must beat, measured on the 40 fresh questions with the deterministic engine and no model: correct **72.5%**, false answers **19.4%**, citation correct 85.3%, conflict detected 40% (2 of 5), abstention precision 40% / recall 33%, missing-information 2 of 6, unnecessary abstention 3.4% (`runs/llm/fresh.deterministic.json`). The harness reports all seventeen requested measures when run; acceptance rules are *proposed* in `docs/model-selection.md`, not agreed.

## 6. Fresh holdout results

**None: not run, on purpose.** Two new sets (24 Acme and 25 Beacon questions) were written from the businesses' knowledge before any benchmark result was used for a decision, sealed with sha1 (checked by a test), compared by program against every other set (no equal question, none at word-overlap ≥ 0.7; the sealed sets were never displayed), and never given to the engine or any model. They are run once, with `--final`, for the model chosen on the other sets; every run is written to a ledger. The fresh sets are model-selection data now. Limits: one author, two small fictional English businesses, 49 questions.

## 7. Security audit

`docs/security.md`: threat table with evidence, cross-tenant attack table, what was found and fixed (privilege gaps → migration 11, membership by invitation, unpacking budget, connection cap, retention job, scanning hook), and what is **not** verified: no live Supabase/Vercel/container, no independent review, penetration test or dependency scan, injection patterns heuristic, ClamAV not deployed, retention scheduler absent.

## 8. Supabase validation results

* **Staging (the owner's declared project `cmgkjtwhbwssrojuxmun`): not validated.** The owner confirmed it is a new project for this platform. A read-only anonymous probe found none of the 43 engine tables and functions there: **migrations 1–13 are not applied**. `scripts/validate_supabase.py` needs `STAGING_DATABASE_URL`, which is MISSING. The project's service key was not used. No production project (washo, Chessmentor) and no other product's project was touched; the validator refuses them.
* **Local PostgreSQL 18 + pgvector with every migration and a stand-in for Supabase's roles:** **53 passed, 0 failed** (53 checks), labelled in its own first line as not a staging validation. Supabase runs PostgreSQL 17, so version differences are exactly what a staging run is for.

## 9. Consent and provider verification status

Consent: built and tested at every layer (database, service per call, screens): per business, provider and disclosure version, owners/admins only, revocation effective at the next request, history undeletable, audited. Provider verification: the checklist, the enforcement and the disclosure text exist; **0 of 6 entries are verified**, so no external provider can be offered, agreed to or called. Verifying means a person reading each provider's official terms for the tier in use and recording them (`docs/provider-verification.md`). Nothing in the repository states what any provider's terms say.

## 10. Customer workflow status

| Step | Status |
|---|---|
| Register, create a business | built; tested through the database and the page's sandbox; not on a real Supabase Auth |
| Invite people, roles | built (link shown once, no e-mail); tested end to end including refusal cases |
| Upload, progress, re-index, remove | built; tested (hosted flows, unchanged content detected, removal deletes facts) |
| Studio, citations | built; tested; the hosted app's History and Test-it views show mode, provider, model, who generated the answer and any fallback or failure reason; the Studio's "How this was decided" shows the pipeline's execution record |
| Mode selection, provider settings, consent | built; tested; the consent screen offers nothing until a provider is verified |
| Local companion | built and security-tested; **run once with a real Ollama and a real `llama3.2:1b` on this Mac (smoke test)**; nothing else real |
| Usage, history, settings | built; tested |
| Desktop and mobile layouts | hand-checked in a browser at desktop and 375 px against the preview stand-ins (not a real backend); the check found tables that made the page scroll sideways on a phone; fixed and covered by a test |
| No decorative controls | every control on the new screens is wired to a call or a state, checked by the screen tests; a screen offers nothing when it cannot tell where the business stands |

## 11. Usage and billing

`docs/usage-and-billing.md`. Metering per mode with tokens and estimated cost (unknown prices stay unknown); plans `trial` / `free` / `paid` with explicit monthly request, token and cost limits (empty means no platform inference, not unlimited); per-minute abuse limit; shared platform cap. **No prices are set and no payment system is added** (as instructed).

## 12. Performance

`docs/performance.md`, two full runs on an 8 GB fanless laptop, synthetic FAQ knowledge, local database: roughly **200–450 ms** median per answer at 200–3,000 FAQs in the long runs (74 ms in a short isolated profile of the same size; the difference is unexplained), **about 3–6 requests per second per process** with no scaling across threads, ingestion **about 40–70 FAQs per second**, peak memory 428 MB, at most 10 database connections under 16 concurrent requests, and about **120 ms** added at the median by the model path with an instant stand-in provider. Not measured: real providers, a hosted database, documents at scale, many businesses at once.

## 13. Fast test results

latest fast run (after the fast path, 2026-11-02) **918 passed, 20 skipped, 0 failed** (5 min 10 s); web **87 passed**, type-check clean; before it, after the real-device smoke test and its fixes, 878 passed, 20 skipped (5 min 13 s); the run before that had one failure, a test that pinned the number of migrations at twelve, updated on purpose for migration 13. An earlier fast run, before the local-first correction, had 824 passed and 1 failed (a Studio test that pinned the provider list at four; OpenRouter was added for BYOK on purpose) (Python, `pytest tests`); the web build passes (the web count before the fast path was 85). The first full run had one failure (a Studio test pinned the provider list at four; OpenRouter was added for BYOK on purpose; the test and the web allowlist were updated and the page's dropdown gained the option).

## 14. Slow test results

**938 passed, 0 skipped, 0 failed** (16 min 44 s, 2026-11-02, after the fast path and the interface redesign; the run before it, after the local-first correction, was 894 passed) (`FAQENGINE_SLOW=1 pytest tests`).

## 15. Integration and end-to-end results

Integration: the hosted service on the real schema with the real embedding model and stand-in providers over HTTP (`test_hosted*.py`, `test_ai_modes.py`, `test_unified_hosted.py`). End to end: `tests/test_e2e.py` runs sign-up, invitation (refusing a stranger and replay), upload and re-read, cited answers, a question the knowledge cannot answer, BYOK without and then with consent, provider failure with honest labelling, withdrawal, local mode, usage and history, changed knowledge, a removed source, and a stranger who sees nothing: passes. **No smoke test against a deployment**: none exists.

## 16. Deployment status

Nothing deployed: no Vercel project, no container image (never built; Docker is not installed here), no Supabase project with the migrations. `docs/vercel-deployment.md` has the environment variables (including the new AI-mode, platform, scanner and logging ones) and the steps; `docs/operations.md` the runbooks.

## 17. Production-readiness gates

| Gate | Status | Why |
|---|---|---|
| 1 Knowledge foundation | **BLOCKED** | ingestion, retrieval, freshness PASSED; tenant isolation validated locally only |
| 2 Reasoning quality | **FAILED** | 19.4% false answers; conflicts 2/5; missing information 2/6; the holdout is unrun |
| 3 AI execution modes | **BLOCKED** | the local-first order and its consent/limit/cap enforcement PASSED against stand-ins (`docs/local-first.md`); **a real local model through the chain, and any real hosted provider, have not been run** (no runtime on the only machine; no credentials; 0 of 6 provider terms verified) |
| 4 Security | **BLOCKED** | staging validation not possible yet; local audits and attack tests PASSED |
| 5 Provider compliance | **BLOCKED** | terms unverified; mechanism PASSED |
| 6 Customer product | **BLOCKED** | not run against a real backend |
| 7 Operations | **BLOCKED** | alerts not delivered, no scheduler, rollback and restore untested |
| 8 Release | **BLOCKED** | web build PASSED; engine image never built; nothing deployed |

Item-level evidence: `docs/production-readiness.md`.

## 18. Known limitations

* Reasoning quality is not good enough to rely on without a verified model helping: false answers 19.4% and weak conflict and missing-information handling on fresh questions; the evaluation sets are small, English, fictional and written by one author.
* Measured locked/dev gap on the unified set: 78.0% vs 93.8%.
* One engine process handles a few requests per second on this machine; the cause of the large run-to-run latency variation is not established.
* About 18 embedding calls per answer dominate the time; not optimised (no casual retrieval changes).
* Local mode: macOS Apple silicon only has run; Linux/Windows detection tested on recorded output only; model fit is estimated, not measured; licences unrecorded.
* No connection pooling in the engine; large uploads are processed inside one request.
* Invitations are links shared by hand; no billing; costs are estimates.
* The function-level rate limit is per instance; retention needs a scheduler; backups outlive deletions.

## 19. Outstanding external dependencies

A Supabase staging project with migrations 1–13 applied and its database connection string; benchmark credentials (`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `OPENROUTER_API_KEY`) and, later, platform credentials (`PLATFORM_*`); a person to read and record each provider's terms; agreed acceptance thresholds; a machine able to run a local model; Docker and a container host; a Vercel project; a scheduler, an alert channel, a ClamAV sidecar; an independent security review; real customer questions for a better holdout.

## 20. Exact next actions required from you

1. **Staging database.** Either (a) in the account that owns `cmgkjtwhbwssrojuxmun`, run `supabase link --project-ref cmgkjtwhbwssrojuxmun` then `supabase db push` (migrations 1–13) and add `STAGING_DATABASE_URL` and `STAGING_CONFIRM_REF` to `~/.config/faq-engine/.env`; or (b) tell me in chat that I may apply migrations 1–13 to that project, and add the same two variables. Then I run `scripts/validate_supabase.py` and the two-account API steps in `docs/staging-validation.md`.
2. **Benchmark credentials.** Add `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` and `OPENROUTER_API_KEY` to `~/.config/faq-engine/.env` (never in chat). `python scripts/env_status.py` will then say SET. I then run the matrix on the fresh evidence, and the holdout once for the model you pick.
3. **Decide the acceptance thresholds** proposed in `docs/model-selection.md` (false-answer limit first).
4. **Provider terms.** Read the official terms of any provider you want offered, for the tier you would use, and fill its entry (checklist in `docs/provider-verification.md`); or tell me which providers to prepare the checklist for. I will not write terms myself.
5. **Local AI.** Run the companion on a machine with more memory or a GPU (`python -m faqengine companion recommend`), so the real-model end-to-end check can be done.
6. **Deployment.** Create the Vercel project and the engine host; set the variables in `docs/vercel-deployment.md`; add a daily call to `/v1/maintenance/purge`, a monitor on `/v1/alerts`, and a ClamAV sidecar.
7. **Tidy up.** Delete the old key copies at `/Users/apple/Desktop/env` and `/Users/apple/Documents/.env` (all keys are now only in `~/.config/faq-engine/.env`).
8. **When you are satisfied,** tell me explicitly to commit; nothing has been committed or pushed.
