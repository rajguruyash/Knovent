# Production readiness

**This system is not declared production-ready.** Eight release gates were set for it (below). Evidence exists for much of what each gate asks, but **every gate has at least one item that is BLOCKED or FAILED**, so none is PASSED as a whole. The blockers are not mostly code: a Supabase staging project that has the migrations, provider credentials, a human reading of providers' terms, suitable hardware for local models, and a decision on the acceptance thresholds. Nothing has been deployed anywhere; "tested" means tested against a local PostgreSQL with every migration, the real embedding model, and stand-in providers over real HTTP.

Status words, used strictly: **PASSED** (executed, with evidence cited), **FAILED** (executed, and the result does not meet the requirement), **BLOCKED** (cannot be executed or decided until a named external thing exists), **NOT APPLICABLE**. A requirement met only against stand-ins or a local database says so in the same cell. **Deterministic mode (no model, nothing leaves the service) is available regardless of any gate**: it is the default, needs no consent and no provider, and every AI setting falls back to it.

## The release gates (2026-11-02)

### Gate 1 — Knowledge foundation: **BLOCKED** (one item)

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| All required ingestion paths tested | **PASSED** | FAQ tables, prose tables, workbooks (every sheet), nested JSON, Markdown/text/HTML/PDF/Word, web pages; unreadable files reported without disturbing anything: `test_unified_ingestion.py`, `test_sources.py`, `test_web.py`, `test_hosted*.py`; upload hardening (scanner hook, unpacking budget): `test_hardening.py` |
| Unified retrieval validated | **PASSED** (with the gap stated) | one ranking for every source kind; Acme unified sets: dev 93.8% correct, **locked 78.0%** (`docs/unified-knowledge.md`): the 15-point gap is the honest generalisation figure, and the sets are small and written by one author |
| Freshness and supersession validated | **PASSED** | expiry, not-yet-valid and replaced material never used as current, on a moving clock, through the hosted service: `test_unified_hosted.py`, `test_hosted.py` (migration 09) |
| Tenant isolation validated | **BLOCKED** | PASSED on a local PostgreSQL (RLS as each kind of person; zero cross-business leakage over 98 questions; `docs/security.md` attack table). **Not** validated on a Supabase project: none has the migrations (`docs/staging-validation.md`) |

### Gate 2 — Reasoning quality: **FAILED**

| Requirement | Status | Evidence |
|---|---|---|
| Fresh holdout evaluation completed | **BLOCKED** | the holdout exists (49 questions, sealed by sha1, `docs/model-selection.md`) and has deliberately **not** been run: it is for the model chosen on the other sets, and none can be chosen without credentials |
| False-answer rate within the agreed threshold | **FAILED** against the proposed threshold; no threshold is agreed | deterministic engine on the 40 fresh questions: **19.4%** false answers (proposed limit 10%); on a second pair of fresh sets 19–20% |
| Citation correctness within the agreed threshold | **not decided** | 85.3% on the fresh set; no threshold agreed |
| Conflict handling validated | **FAILED** | conflict detected 2 of 5 (40%) on the fresh set |
| Missing-information handling validated | **FAILED** | correct 2 of 6 on the fresh set; abstention precision 40%, recall 33% |

The weak categories were deliberately not tuned against the fresh sets (they are model-selection data now). The pipeline lets a verified model help with exactly these; whether one does is the benchmark that is BLOCKED.

### Gate 3 — AI execution modes: **BLOCKED**

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| Platform-hosted mode works | **BLOCKED** | works end to end against a stand-in provider over HTTP (credentials from the service's environment, plan limits, metering, breaker): `test_ai_modes.py`. No real provider has been called: no credentials, no verified terms, no model with `platform_offered` |
| BYOK works | **BLOCKED** | same: against stand-ins for the OpenAI-style, Anthropic and Gemini request shapes and an OpenAI-compatible server (`test_inference.py`, `test_ai_modes.py`, `test_e2e.py`); encrypted write-only key, SSRF rules. No real provider key has been used |
| Local AI works on explicitly supported configurations | **PARTLY: one configuration smoke-tested** | macOS 15.2 / Apple M2 / 8 GB / Ollama 0.40.13 / `llama3.2:1b`: pairing, download, selection, registration, health, 3 real questions, cancellation, no outside request (`docs/local-first.md`). Not run: any other model, llama.cpp, Linux, Windows, a GPU, CPU-only; answer quality is not benchmarked and, on three questions, the 1B model's answers were less complete than the deterministic ones. 71 companion and check tests cover the protocol and security |
| **Local first: the order is local → (own provider account) → free hosted → paid hosted, every outside step is off by default and used only with verified terms, the business's agreement for that provider and its limits** | **PASSED** (stand-ins) | `tests/test_local_first.py` (23): what each provider received is read from its side; a refused step sends nothing; a failed provider hands over only to a step the business enabled; a bad answer or a Cancel hands over to nothing; free before paid; the paid step stops at the business's own spending cap; turning a step on needs the agreement first; the audit trail records who turned it on. `docs/local-first.md` |
| **A complete, verified answer is not sent to a model (the fast path)** | **PASSED** (stand-ins and the real example knowledge) | `tests/test_fast_path.py` (28) and `tests/test_fast_path_hosted.py` (12): each of the eight conditions alone stops it; a conflict, an outdated entry, a partial multi-part answer, an insufficient one, a clarification, another business's entry and a failed verification never take it; a similarity score is never an input; the provider receives nothing for an exact answer and consent is still enforced where a model is needed. Measured on 174 development/regression/fresh questions: 65 skipped the model, 64 correct (98.5%), citations 100%, **identical to the deterministic baseline**; one wrong structured answer inherited from the baseline (reported, not tuned). `docs/fast-path.md` |
| Local-first setup: companion detection, hardware-aware recommendation of several small, fast models, one-confirmation download, model list/update/remove, storage and health | **PASSED** against the stand-in runtime; **BLOCKED** on a real one | 64 companion tests, 14 page tests, `scripts/local_e2e_check.py` (tested against the stand-in). Nothing has been downloaded or run on a real Ollama here |
| **A real local model executed through the whole chain on real hardware** | **PASSED as a smoke test, on one computer** | `scripts/local_e2e_check.py --provision llama3.2-1b --yes` (reports in `runs/local-e2e/`): downloaded 1.23 GB in 277 s after a disk, memory and size check, answered through the real pipeline in 1.1–14 s per question (the two runs differ tenfold, unexplained), cancelled in 0.01–0.05 s, ~300 MB resident for Ollama and ~1.2 GB of reclaimable memory used, swap unchanged, only `127.0.0.1` contacted outside the download. It shows the chain works; it is not a quality or reliability claim |
| All modes use the same reasoning pipeline | **PASSED** | one pipeline; `generated_by`/`mode` on every answer; same frozen evidence per provider (`test_ai_modes.py`, `test_hosted_pipeline.py`) |
| Fallback and privacy policies enforced | **PASSED** | router order, no failover to another provider/mode/model, `fallback: deterministic\|none`, never "verified" after a lost claim, local never reaches a provider, nothing decrypted before consent/limits pass (`test_ai_modes.py`, `test_hosted_pipeline.py`) |

### Gate 4 — Security: **BLOCKED**

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| Real Supabase staging validation passed | **BLOCKED** | the owner declared a project; its anonymous API shows **none of the engine's tables** (migrations not applied). `scripts/validate_supabase.py` (53 checks) needs `STAGING_DATABASE_URL`; no production or other-product project was touched |
| RLS and privilege audits passed | **PASSED locally; BLOCKED on Supabase** | the same 53 checks pass on the local PostgreSQL (labelled as not a staging validation); the audit found and migration 11 closed four privilege gaps |
| Consent enforcement passed | **PASSED** | per call, per business/provider/disclosure version, revocation effective at the next request, an unverified provider unusable (`test_ai_consent.py`, `test_hosted_pipeline.py`, `test_ai_modes.py`, the validator) |
| Credential security passed | **PASSED** (local) | AES-256-GCM bound to the business, write-only, never logged or returned, decrypted last (`docs/security.md`) |
| Cross-tenant attack tests passed | **PASSED** (local) | `docs/security.md` table |
| (no independent review) | **BLOCKED** | no external security review, penetration test or dependency scan exists |

### Gate 5 — Provider compliance: **BLOCKED**

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| Provider terms verified | **BLOCKED** | **0 of 6** entries verified: it needs a person to read each provider's official terms for the tier in use and record them (`docs/provider-verification.md`). Nothing in the repository states what any provider's terms say |
| Correct disclosure displayed | **BLOCKED** | the screen shows exactly the backend's text, as text, with provider, version and what may be sent, and says what is unverified (`web/tests/consent_ui.test.js`); there is no verified text to display yet |
| Consent and revocation tested | **PASSED** | database, service and screen |
| No unverified provider enabled | **PASSED** | none is verified, none is offered, agreed to or called; `ENGINE_AI_MODES` is empty by default |

### Gate 6 — Customer product: **BLOCKED**

Sign-up and business creation, invitations, role-limited Settings, knowledge upload and re-read (a file of secrets is refused), the first-run choice (*on my computer*, *through an API service* with a dropped `.env` file, or *no AI*), the local-first policy chooser, BYOK provider settings, consent screen, companion pairing, automatic local setup, model management and health, Test it, History (which step answered and which were tried), Usage and plan with free/paid tiers and the spending limit: built, covered by 87 web tests (screens run in a sandbox with a DOM stand-in) and by the Python flows; the screens were driven in a browser against `web/scripts/preview.mjs`'s stand-ins, and on 2026-11-02 the first-run setup was walked end to end in a browser against a **real companion over a stand-in Ollama** (pair, one-confirmation setup, local-only saved, the guarded refusal of a hosted step without agreement, phone width, a dropped `.env` file). **Not** run against a real Supabase, Auth or Storage, so onboarding, upload and provider settings are not PASSED as customer-product requirements. Customer-facing error handling is tested (plain messages, no stack traces or secrets, `web/tests/api.test.js`).

### Gate 7 — Operations: **BLOCKED**

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| Monitoring and alerts configured | **BLOCKED** | health, readiness, metrics and 11 alert rules exist and are tested; **nothing polls them**, no channel is configured |
| Usage and cost tracking works | **PASSED** | per mode, tokens and estimated cost, limits enforced (`docs/usage-and-billing.md`); cost is an estimate, not an invoice |
| Rollback tested | **BLOCKED** | flag and fallback paths tested in code; redeploying a previous image, `ENGINE_AI_MODES=` on a running service and a database restore have not been run anywhere |
| Backup and recovery documented | **PASSED** (documented) | `docs/operations.md`; the restore has never been tested |
| Deployment runbooks available | **PASSED** | `docs/operations.md`, `docs/vercel-deployment.md` |

### Gate 8 — Release: **BLOCKED**

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| Production build passes | **PASSED** for the web app (`npm run build`, type-check clean); **BLOCKED** for the engine image | `deploy/engine/Dockerfile` has never been built (no Docker here) |
| End-to-end smoke tests pass | **PASSED** locally; **BLOCKED** deployed | `tests/test_e2e.py` (sign-up to changed answer, every mode); no deployment exists to smoke-test |
| No critical security issues | **not claimable** | none known; none could be known without independent review and a live environment |
| No unresolved critical regressions | **PASSED** at the time of writing | see the test table below |
| Production configuration independently verified | **BLOCKED** | no production configuration exists |

## Test results (2026-11-02, one Apple-silicon laptop, 8 GB)

| Suite | Result | What it covers |
|---|---|---|
| Python fast suite (`pytest tests`) | latest fast run (after the fast path, 2026-11-02) **918 passed, 20 skipped, 0 failed** (5 min 10 s); before it, after the real-device smoke test and its fixes, 878 passed, 20 skipped; the run before that had one failure, a test that pinned the number of migrations at twelve, updated on purpose for migration 13. An earlier fast run, before the local-first correction, had 824 passed and 1 failed (a Studio test that pinned the provider list at four; OpenRouter was added for BYOK on purpose) | everything below except the slow model tests |
| Python everything (`FAQENGINE_SLOW=1 pytest tests`) | **938 passed, 0 skipped, 0 failed** (16 min 44 s, 2026-11-02, after the fast path and the interface redesign; the run before it, after the local-first correction, was 894 passed) | adds the model-training and package tests |
| Web (`cd web && npm test`) | **87 passed** (type-check clean) | API functions (authentication, roles, whitelisting, error mapping, rate limit, no secrets), consent screen, mode/usage/history screens, companion pairing in the page, CORS/CSRF, static checks of everything served and of `vercel.json` |
| Web type-check (`npx tsc -p jsconfig.json`) | clean | the API functions and build script (strict JSDoc types) |
| Web production build (`npm run build`) | passes | |
| Supabase validator on the local PostgreSQL (`scripts/validate_supabase.py --allow-local`) | **53 passed, 0 failed** | 53 catalog and behaviour checks; **not** a staging validation |
| Lint (ruff, real-error rules) on the files this stage touched | clean | pre-existing unused imports remain in unrelated files |

New tests of this stage: `test_local_first.py` (23), `test_local_e2e_script.py` (3), web `local_first_ui.test.js` (15), plus `test_inference.py` (41), `test_ai_modes_schema.py` (17), `test_ai_modes.py` (35), `test_telemetry.py` (8), `test_companion.py` (53), `test_probe_supabase_anon.py` (13), `test_env_status.py` (5), `test_staging_validator.py` (35), `test_hardening.py` (9), `test_ssrf.py` (48), `test_e2e.py` (1 narrative), `test_llm_bench.py` (13), plus web `security.test.js` (5) and others. No test was removed or weakened: tests that depended on a retired path (direct member inserts, the legacy hosted model call) were rewritten with equivalent assertions for the new path, and one list (`test_studio_llm.py`) gained OpenRouter on purpose.

Hand checks in a browser, against `web/scripts/preview.mjs`'s stand-ins only (never a real Supabase or engine): earlier, the landing page, onboarding, key check, answer and refusal; on 2026-11-02, the new Settings screens at desktop and phone width (how the business answers, own provider, companion, consent read → tick → agree → "Agreed · in use" → withdraw offered, usage, team). That check **found and fixed a real defect**: the usage and team tables sat in a wrapper (`.scroll`) that had no style, so at 375 px the whole page scrolled sideways (462 px wide); the style was added, a test now guards it, and the page measures 375 px. The console showed only the expected refused connections to the companion's port (no companion running).

## What is completed (built and tested)

* **Hybrid retrieval** in one ranking for every kind of source (no ranking advantage for FAQ entries), with structured candidates, duplicate collapsing, source filtering, a guard for the evidence pool, and out-of-date material kept out (`docs/retrieval-architecture.md`). Cross-encoder not integrated; its experiment is preserved.
* **One ingestion pipeline for every kind of source:** FAQ tables, prose tables, tables of values, every sheet of a workbook, nested JSON, Markdown/text/HTML/PDF/Word with their tables, web pages; any header names; source, heading and address kept; a file that cannot be read is reported and disturbs nothing (`docs/unified-knowledge.md`).
* **Freshness:** `valid_from` / `valid_until` / `supersedes` from a file's header or set by a person; expired, not-yet-valid and replaced material is never used as current, and a question only it would answer is told so (migration 09, `runtime/validity.py`).
* **Evidence assessment** independent of any model (`docs/evidence-architecture.md`), with four fixes found by tests this stretch.
* **Response composition** with six outcomes, including a narrow, deterministic clarification.
* **Optional language-model layer:** four providers' connection code, key check, polish / combine / retrieval assistance, claim-level verification, injection quarantine, fallbacks that are recorded, usage without invented cost (`docs/llm-integration.md`).
* **Local-only mode** proved offline (`tests/test_offline.py`).
* **Studio onboarding** of the optional model, per project, key kept server-side and out of exports.
* **Supabase schema** (9 migrations; the ninth adds freshness and the `knowledge_units` view): isolation, roles, locked columns, encrypted key table, storage policies, evaluation integrity, release gate and rollback, audit trail, deletion (`docs/supabase-architecture.md`).
* **Hosted engine service** on that schema: ingest with atomic replacement, answering, isolation, roles, per-business rate limit, privacy setting, key encryption bound to the business, HTTP layer.
* **Vercel API layer** and static checks.

## Phase G and the reasoning pipeline (added 2026-11-02; details in `docs/reasoning-pipeline.md`; the "Not built" line is superseded by Phase J below)
* **Built and tested:** structured reasoning over table rows (comparisons, filters, counts, sums, price arithmetic, limit checks, conjunctions), claim-type verification, dates inside sentences, paraphrased conflicts, multi-sentence answers, and the model-independent pipeline (query plan → evidence ids and validation → sufficiency → one targeted retrieval → composition → claim-level verification) with deterministic mode, mock providers and server-side consent checks.
* **Not built:** any real provider, the router, per-business AI configuration and consent storage, wiring into the hosted service and Studio, the Studio AI Configuration screen, the desktop companion. **No language model has been run through this pipeline; the model benchmark is blocked on Cloudflare/OpenRouter credentials.**
* **Honest numbers:** the sets the engine was developed against score 100%; on two fresh 20-question sets it scores 70% and 75% with 19–20% false answers (missing information and conflicts are the weak categories).

## Phase H: the pipeline in the hosted service and the Studio, and AI consent (added 2026-11-02; details in `docs/reasoning-pipeline.md` and `docs/ai-disclosure.md`; the "Not built" line is superseded by Phase J below)
* **Built and tested:** migration 10 (`ai_inference_consents`, revoke-only, owner/admin only, audited, isolated per business); server-side consent check before every external call (`DbConsent`, never cached); the business's own configured model gated by the same consent (its key is not decrypted without it); consent and disclosure routes (`/v1/ai/*`); `ENGINE_PIPELINE` / `FAQENGINE_PIPELINE` (`off`/`shadow`/`on`) with automatic fall-back to the existing path; shadow comparison with mismatches recorded without text; `execution` on every answer (path, mode, `generated_by`, failure kind, passes, stage latency); the Studio's "How this was decided" view; disclosure text per provider that says what has not been verified.
* **Measured, not assumed:** shadow parity on the hosted data (57 Acme and 77 Beacon dev/regression questions, expiry and replacement on a moving clock) and on the Studio's evidence example. The first run found 8 of 57 differences, all explained and handled in the comparator (see `docs/reasoning-pipeline.md`), none a difference in outcome, answer or cited source.
* **Not built (by instruction):** any provider integration, provider selection screen, Cloudflare/OpenRouter code, the local companion, or any setting that enables AI inference for a customer. **No external model is called by this product.** The Vercel functions do not proxy the consent routes yet; no screen exists to read the disclosure and agree.
* **Not verified:** migration 10 on a live Supabase project; `shadow` on live traffic; every provider's data-handling terms (the disclosure entries are empty and say so).

## Phase J: the three execution modes, completed on the same pipeline (2026-11-02)
* **Built and tested:** a provider-neutral adapter contract with OpenAI-style, Anthropic, Gemini and Ollama adapters, parameter negotiation with the same model, failure classification, retries, timeouts and circuit breakers (`src/faqengine/inference/`); a server-side router (operator switch → business mode → verified terms → consent → limits → breaker; no failover); platform, BYOK and local modes through the one pipeline; plans, per-mode metering, per-minute abuse limits and a platform concurrency cap; migration 11 (privilege hardening) and 12 (mode settings, plans, usage columns, companions, invitations); correlation ids, structured logs, readiness, metrics, 11 alert rules; the local companion (loopback service, pairing, signed requests, real cancellation, hardware detection and honest model fit); the Settings screens for modes, consent, usage, history, companion and team; retention purge, upload scanning hook, unpacking budget, connection cap; SSRF, CORS/CSRF and cross-tenant tests; an end-to-end narrative test; the benchmark harness on the production adapters with the seventeen measures and a sealed final holdout; performance measurements. See `docs/architecture.md`, `docs/local-companion.md`, `docs/operations.md`, `docs/usage-and-billing.md`, `docs/model-selection.md`, `docs/security.md`, `docs/performance.md`.
* **Not done, and why:** no real provider was called (no credentials, no verified terms); the model benchmark and the holdout run are blocked; the Supabase staging validation is blocked (migrations not applied to the declared project); local mode has not been run with a real model; nothing is deployed.

## Phase K: the local-first correction (2026-11-02, later)
* **Built and tested:** the ordered plan (local → own account → free hosted → paid hosted, every outside step off by default) and its enforcement (`tests/test_local_first.py`, 23); migration 13; free hosted's own quota and paid hosted's own spending cap; per-call tiers in usage; the settings API with named policies and per-step validation (refused unless the provider is verified and the business already agreed; the paid step needs a cap); local completion that continues only to steps the business enabled and never after a cancel; the page's first-run choice, policy chooser and "which step answered / which were tried" line; the companion's wider small-model list with speed classes, one-confirmation automatic setup, model list/update/remove, storage, health and CPU option (64 companion tests); the `.env` drop and the refusal of secret files as knowledge (browser and engine); the real-device check script (tested against the stand-in); the validator's two new checks (53). A real-browser walk-through against a real companion over a stand-in Ollama found and fixed one defect (the Settings tab crashed when the engine's usage response predated the free/paid split).
* **Not done, and why:** a real model has run through the chain **once** (the smoke test above) but nothing else about local AI has been verified on real hardware, and the small model's answers did not beat the deterministic ones on three questions (no benchmark has been run); no real provider for the hosted steps (no credentials, 0 of 6 provider terms verified); no automatic CPU retry after a GPU error; no packaged companion installer; Linux/Windows companion paths and non-Chrome browsers untested.

## Phase L: the deterministic fast path, and the interface (2026-11-02, later still)
* **Built and tested:** `Pipeline.evaluate_fast_path` / `conclude_fast` (about 90 lines in `runtime/pipeline.py`, no second retrieval): an exact FAQ answer or a fully supported structured answer skips the model only when it is understood, complete for every part, current, of this business, free of conflicts and passes claim and citation verification; every answer records `answer_path`, `fast_path_reason`, `model_invoked`, `provider_steps_tried`, evidence ids, the verification outcome and its latency; `ENGINE_FAST_PATH=on|off` (default on); applied to server-side steps and to the two-phase local flow; 40 new tests, 53 existing model-path tests now build their engine with `fast_path=False` (assertions unchanged); `scripts/fast_path_report.py` (development, regression and fresh sets only). The web interface was redesigned (`web/public/site.css`, `index.html`) as an original dark product design; this surfaced and fixed a real defect: `setAttribute('style')` was blocked by the production CSP, so styles the app set from script silently did nothing (it now uses the CSS object model, proved in a browser under the real CSP headers, and a test forbids inline styles); landing page and app were checked in a browser at desktop and phone widths (no horizontal overflow).
* **Not done, and why:** the sealed locked, verify and holdout sets were not run (the script cannot read them); only 7 of the 65 skipped questions are the genuinely new exact-FAQ skip (the other 58 were never sent to a model); one wrong structured answer on a fresh question is inherited from the baseline and was deliberately not tuned (fresh sets are model-selection data); no real provider was called; the fast path is not in the Studio; the interface was not checked in a second browser or on a physical phone, and neither Webflow template the owner named could be opened, so the design is original (`docs/final-report.md`).

## Implemented, but verified less than it should be

| Item | Why only partly verified |
|---|---|
| Hosted web app (`web/public/app.js`) | run only against stand-ins (`web/scripts/preview.mjs`) and by hand; no automated browser test; never against a real Supabase |
| Studio's new screens | exercised by hand with a stand-in provider; the back end is tested, the page is not |
| `deploy/engine/Dockerfile` | never built (no Docker) |
| The four providers' connection code | tested against HTTP stand-ins that reproduce each API's shapes; **never against a real provider** |
| Verification layer | measured on 510 tampered and 222 honest rewrites of stored answers, **after being fixed on the same answers** (so optimistic; the first, unfixed run missed 6 and refused 7); the rewrites are mechanical, not a real model's mistakes |
| Quarantine patterns for instruction-like text | heuristic; novel phrasing will pass retrieval (the verification is the real defence) |
| Retrieval at scale | measured to 20,000 synthetic entries: 72 ms median search, before the ranking was unified (a candidate list of 10 instead of 5 + 5 changes little, but it was not re-measured); nothing larger; the keyword channel is pure Python |
| The unified benchmark | 98 questions about one fictional business, written by the rules' author; dev 93.8%, locked 78.0% correct (`docs/unified-knowledge.md`): the gap is the honest figure, and the locked set has 41 questions |
| The tuned-versus-pretrained encoder comparison for passages | three tuned encoders, 57 + 21 questions: no difference found, which rules out nothing large and proves nothing small |
| Hosted web page for freshness (the *Dates* button, the *Current* column, the out-of-date notice) | static checks only; not run in a browser against a real Supabase |

## Partially implemented

* **pgvector:** the column, HNSW index and `match_embeddings` exist; the service ranks in memory instead (one implementation of the rules) and does not call them.
* **Retention:** `privacy.retention_days` is enforced by `POST /v1/maintenance/purge` (tested); **no scheduler calls it yet**.
* **Members:** roles are enforced; people join by **invitation** (a link whose secret is shown once to the inviting admin; migration 12, a Team screen). No e-mail is sent: the admin passes the link on.
* **Column choice:** tables with arbitrary header names are read in the hosted service too (by what the columns hold), but the manual *Columns* override exists only in the local Studio.
* **Freshness inputs:** a header can state validity only in Markdown and text files; for other kinds a person sets the dates in the hosted app, and the local Studio has no screen for them. No scheduled re-crawl; nothing infers staleness from a page's content.
* **Hosted engine and files:** ingestion of an uploaded file and of a web page works; there is no scheduled re-crawl (re-reading is a button).
* **Usage and cost:** tokens, latency, status, mode and estimated cost (only where a price is recorded) are metered and limited per plan; there is no billing (`docs/usage-and-billing.md`).

## Not implemented

A Vercel Python-function variant of the engine; fine-tuning in hosted mode (hosted uses the shared pretrained model); sending invitation e-mails; billing and payment; a job queue for large uploads; connection pooling in the engine; Edge Functions (none needed); WhatsApp or a chat widget (out of scope by the task); streaming; multi-turn memory; spelling correction and query expansion; per-tenant ANN search.

## Blocked by external credentials or infrastructure

| Needs | To verify |
|---|---|
| A Supabase project and its keys | migrations on the managed Postgres (17), Auth sign-up and sessions, PostgREST behaviour of the column grants and RPC, the Storage API, bucket limits |
| A Vercel account / CLI login | `vercel build`, preview and production deployment, headers actually served, cold starts of the functions |
| Docker and a container host | the engine image, its health check, behaviour on Linux wheels, memory in the host's limits |
| Provider credentials for the benchmark (`CLOUDFLARE_*`, `OPENROUTER_API_KEY`) and for the platform (`PLATFORM_*`) | the model benchmark, the fresh-holdout run, real latency, failure rate and cost per answer (none measured) |
| A person reading each provider's terms | the disclosure entries (0 of 6 verified) |
| A machine able to run a local model (more than 8 GB, or a GPU) | local-mode end to end with a real model; the supported-configuration list |
| A scheduler, a monitor, a ClamAV sidecar | retention enforcement, alert delivery, malware scanning |
| Real customer questions | a held-out set that was not written by the rules' author; real failure rate |

## Evaluation: what was and was not measured

| Requested | Status |
|---|---|
| Recall@1, @5, @10, MRR@10, right-first | measured (`docs/retrieval-architecture.md`, `docs/cross-encoder-experiment.md`, raw numbers in `runs/prod-baseline/retrieval.json`) |
| Evidence precision / recall, sufficiency accuracy, unsupported-evidence acceptance, partial detection | measured as sufficiency precision and recall, false-answer rate and partial handling (`docs/evidence-results.md`); 15 / 6 / 36 questions, so coarse |
| Correct answer rate, coverage, false-answer rate, abstention precision and recall, multi-part completion, conflict detection | measured on two benchmark families. **Pawfect evidence sets (36 locked):** coverage 82.4%, correct 76.5%, false answers 7.1%, multi-part 2 of 2, partial 4 of 5, conflicts 3 of 3 (at the previous milestone 76.5%, 70.6%, 7.7%, 1 of 2, 5 of 5, 3 of 3: one question each way). **Acme unified sets (41 locked):** correct 78.0% (58.5% with the earlier code), false answers 8.7% (23.8%), citations 82.4% (52.9%), conflicts 2 of 3, abstention recall 100% and precision 58.3% (`docs/unified-knowledge.md`). Both are small. |
| Source attribution accuracy | reported separately on the unified sets as *citation correct* (every file the facts came from is cited, none that must not be); inside "correct" on the Pawfect sets |
| Retrieval, embedding, whole-answer latency; indexing time; memory | measured on one machine (`scripts/operational_metrics.py`): embed 0.8 ms; whole answer 74 ms median warm (about 78 ms first time) on a 19-entry knowledge base, p95 132 ms warm; search 0.8 / 8.6 / 72 ms at 1,000 / 5,000 / 20,000 entries; embedding 1,000 views 0.8 s, 20,000 views 80 s; peak memory 161 / 190 / 296 MB |
| LLM latency, cost per generated response, failure rate | **not measured** (needs a real provider; no price configured) |
| Failure rate in operation | **not measured** (no deployment, no traffic) |

## Known limits worth stating again

* The unified benchmark's locked result (78.0%) is 15 points below its development result (93.8%): that is what tuning on a small set costs. A larger set, written by someone other than the rules' author and for businesses other than a fictional one, is the first thing to do.
* The evidence rules are tuned to a dog-grooming vocabulary and a few other domains; a very different domain may need its own words for the kinds of detail it asks about (prices, times, counts…).
* The verification layer is strict (it refuses harmless additions) and structured, not a proof; its blind spots are written in `runtime/verify.py: LIMITS`.
* Local mode stores a model key unencrypted in a protected file.
* The in-function rate limit in `web/` is per instance; use the Vercel firewall for a real limit.

## Rollback

* **Knowledge versions:** `rollback_to_version()` (SQL) after a release gate; a source's earlier reading stays in `knowledge_source_versions` and, inactive, in `document_chunks`.
* **Local packages:** `ci/improve.py` keeps the previous version and swaps only after the regression suite passes.
* **Web:** promote an earlier Vercel deployment. **Engine:** redeploy the earlier image.
* **Schema:** a new migration; never edit an applied one.

## Before a first real deployment

1. Create the Supabase project, apply the migrations, run the migration tests' scenarios by hand against it (sign up two users; try to read each other's data).
2. Build the engine image, run it, run `tests/test_hosted.py` against it, and check `/health`.
3. Deploy `web/` to a Vercel preview, set the four variables, and walk the smoke test in `docs/vercel-deployment.md`.
4. Try each provider you intend to offer with a real key on a throwaway business; read the checks' messages.
5. Write the new development set (above) and a real regression suite, and gate releases on it.
6. Read `docs/unified-knowledge.md`'s list of what does not work (comparison questions, vocabulary gaps, a shared topic word accepted as support, conflicts hidden by paraphrase) and decide which matter for the business's questions.
