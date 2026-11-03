# Operations: monitoring, alerts, runbooks, retention, backup and recovery

What an operator needs to run the engine service and respond when it misbehaves. Everything here is **implemented and tested in code**; nothing has been run against a deployed service (there is none), and the thresholds are starting points chosen without production traffic. Where this page says "set up", that is the operator's step, not something that already exists.

## What the service exposes

| Endpoint | Auth | Says |
|---|---|---|
| `GET /health` | none | the process is up (for a container health check); nothing about traffic |
| `GET /ready` | none | `{ready, checks:{database, schema, embedding_model}}`: booleans only, no address or error text. Use as the load balancer's readiness probe |
| `GET /v1/metrics` | shared secret | counters, latency percentiles, circuit states, which operator switches are on, which platform credentials are `SET`/`MISSING` (never values), pipeline rollout counters. No business, person, question or answer text |
| `GET /v1/alerts` | shared secret | the alert rules below and which are firing now |
| `POST /v1/maintenance/purge` | shared secret | deletes messages older than each business's retention (see below); returns counts only |

The Vercel side has `GET /api/health` (the function is up) and `GET /api/config` (the public Supabase URL and anon key the page needs; never a secret).

## Logging

Structured JSON lines (`ENGINE_LOG_FORMAT=json`, level `ENGINE_LOG_LEVEL`), each with the request's **correlation id** (`X-Request-Id` from the caller if it is well formed, else generated; echoed in the response header and recorded on the answer's `execution` and its usage row, so a customer-reported answer can be traced to its log lines and its usage record). Fields that could carry content or credentials are scrubbed by name before a line is written; the code paths that handle questions, evidence, prompts, provider replies and keys do not pass their content to the logger at all, and tests assert that a key, a question and a passage do not appear in the captured output (`tests/test_telemetry.py`, `tests/test_hosted*.py`). Provider error messages are redacted of the key before they are kept. The companion writes no access log.

## Alert rules (`hosted/alerts.py`, window 5 minutes)

Poll `GET /v1/alerts` and act on `firing`. Delivery (a page, a message) is the monitor's job and is **not set up**.

| Rule | Severity | Fires when | First actions |
|---|---|---|---|
| `answers_failing` | critical | >5% of ≥20 requests ended in a server error | check `/ready`; look at the logs for the correlation ids of failing requests; check the database and the embedding model directory; if a release caused it, redeploy the previous image |
| `pipeline_faults` | critical | ≥3 pipeline exceptions (the existing path answered instead) | set `ENGINE_PIPELINE=off` to stop using the pipeline while the cause is found; the answers continue on the existing path |
| `provider_down` | critical | >50% of ≥10 provider calls failed | see `circuit_open` / `provider_failing`; if a platform provider is down, set `ENGINE_AI_MODES` without `platform` (businesses on it fall back as their `fallback` says: deterministic or "unavailable") |
| `provider_failing` | warning | >20% of ≥10 provider calls failed | read the failure kinds in `/v1/metrics`: `rate_limited` and `quota` are the account's, `auth` is a revoked or wrong credential, `server`/`timeout` the provider's |
| `circuit_open` | warning | a circuit breaker is open (named by mode/provider/model) | calls to it fail immediately and probe again after the cool-down; nothing is routed elsewhere, by design |
| `latency_p95_high` | warning | p95 whole-request time >15 s over ≥20 requests | usually a slow provider; check provider time vs. total in `/v1/metrics`; the knowledge size of the busiest business (`docs/performance.md`) |
| `claims_rejected_often` | warning | >20% of ≥20 model answers lost a claim to verification | a model that invents or paraphrases beyond its evidence: consider switching the platform model (a change to `platform_offered` in the catalogue, after the benchmark) |
| `consent_refusals` | warning | ≥20 AI requests refused for consent/verification | a misconfiguration (a provider's entry went unverified), or a client that keeps asking after a withdrawal |
| `security_events` | warning | ≥5 refused attempts (wrong role, other business, bad secret) | review the audit log and the Vercel logs for the source |
| `quota_refusals` | info | a business hit its plan limit | tell the customer or raise the plan |
| `rate_limit_events` | info | ≥10 requests slowed by a rate limit | a runaway client or an attack; use the Vercel firewall for a hard limit |

## What the service counts about the order answers are tried in

Alongside the counters above, `step_fallback` counts answers a later step made after an earlier one could not (a business's own computer failed and free hosted answered), and `consent_refused` / `quota_refused` / `rate_limited` now count **every refused step**, not only the last. Each answer's log line carries the step that answered (`tier`) and how many steps were tried (`steps`). A rising `step_fallback` for one business means its computer is failing or offline; the alert that matters for the customer's own wallet is `quota_refusals`.

## What the service counts about the fast path

Every answer's `execution` record, its log line and a counter say which path produced it: `answer_path_fast_path` (a complete verified answer existed, no model was asked), `answer_path_model`, `answer_path_deterministic`, `answer_path_abstained` (the evidence does not answer; a model is never asked to fill the gap), `answer_path_deterministic_fallback`, `answer_path_deterministic_no_consent`, `answer_path_unavailable`. The History tab shows the same per answer, with `fast_path_reason` (why it was or was not taken), `model_invoked`, `provider_steps_tried`, the evidence ids and the verification outcome; none of them holds question or evidence text. A rising `answer_path_model` with a falling `answer_path_fast_path` after a knowledge change usually means entries lost exact matches (expired or replaced sources). Roll back with `ENGINE_FAST_PATH=off` and a restart; nothing else changes.

## Circuit breakers and capacity

One breaker per (mode, provider, model); for BYOK also per business, so one customer's bad key cannot open a circuit for others. It opens after repeated failures that describe the provider's health (rate limit, server error, timeout, network) — a customer's rejected key does not count — and half-opens after a cool-down. Platform calls share a concurrency cap (`ENGINE_PLATFORM_CONCURRENCY`, default 8) so one busy business cannot use all of the platform's capacity; BYOK and local calls are not limited by it. Database connections are capped per process (20 by default; `Store(max_connections)`), so a burst waits instead of opening more.

## Retention and secure deletion

* **Questions and answers** are kept only if the business keeps them (`privacy.log_questions`) and, when kept, for `privacy.retention_days` (default 365, clamped to 1–3650; a value that is not a number is the default; 0 means one day, never "delete everything now"). `POST /v1/maintenance/purge` deletes older messages, the feedback on them, and conversations left empty; the audit trail is append-only and is not purged. **A scheduler must call it** (once a day is enough): until one does, nothing enforces retention. Tested on the real schema (`tests/test_hardening.py`).
* **Knowledge** is deleted when its source is deleted (cascade to chunks, embeddings and versions); the source's file in Storage is a separate object and is removed by the app's delete action.
* **Deleting a business** writes a deletion receipt (no content) and cascades; a business's stored provider key is deleted with its row, and a key is also deleted when the provider is changed.
* **Backups are outside the application's reach.** A deleted row remains in the database provider's backups until they expire; that period is the database provider's, and has to be stated to customers from the plan actually chosen. **Not verified**, since no project exists to read it from.

## Uploads

Size limits (200 MB total unpacked per file, per-part limits for archives and workbooks), a file kind the readers do not know is refused, a file that cannot be parsed is reported and disturbs nothing, nothing in a document is executed (no macros, no scripts), passages that look like instructions to a model are flagged and quarantined from prompts. **Malware scanning:** the service speaks the ClamAV `INSTREAM` protocol (`ENGINE_SCANNER=clamav`, `CLAMAV_HOST`, `CLAMAV_PORT`): an infected file is refused with HTTP 422 and the match name (not the file) is recorded in the audit trail; with `ENGINE_SCAN_REQUIRED=1` an unreachable scanner refuses the upload (HTTP 503, fail closed); without it, the file is read and the result says `scanned:false`. **No scanner is deployed or run here**; the protocol is tested against a stand-in that implements `INSTREAM`. Running `clamd` next to the engine (a sidecar) is the operator's step.

## Deploy, rollback, recovery — runbook

**Deploy** (order matters): 1) back up / take note of the database state; 2) apply new migrations to **staging** and run `scripts/validate_supabase.py`; 3) deploy the engine image to staging, check `/ready`, run the smoke test (`docs/vercel-deployment.md`); 4) deploy `web/` preview; 5) only then production, after the owner confirms the target project (the validator refuses a project that is not the declared staging one; production migrations need the owner's explicit confirmation).

**Roll back**: web — promote the previous Vercel deployment; engine — redeploy the previous image; `ENGINE_PIPELINE=off` returns answering to the earlier path without a redeploy; `ENGINE_FAST_PATH=off` makes every question in an AI mode go to its model again; `ENGINE_AI_MODES=` (empty, which is also the default) switches every AI mode off at once, leaving deterministic answering; schema — never edit an applied migration, add a new one (a rollback migration for 11 and 12 has not been written, because dropping privilege hardening is not a rollback anyone should want by default).

**Recover**: the database provider's backups (point-in-time recovery depends on the plan chosen — to be confirmed and **tested by restoring into a scratch project** before relying on it). Stored provider keys are encrypted with `LLM_CREDENTIAL_KEYS`; **losing that key makes every stored customer key unrecoverable** (customers re-enter them), so keep it in the host's secret manager with a second copy held by a second person. Rotation: set `LLM_CREDENTIAL_KEYS="2:<new>,1:<old>"`; each key moves to the newest version when re-saved.

**Incident: a suspected leak of a provider key** — the customer deletes the key at the provider; remove it in Settings (it is deleted from the database); for a platform credential, rotate it at the provider and in the service's environment, then restart. The service never logs or returns a key, so there is nothing to purge from logs.

**Incident: a provider's terms changed** — mark its entry unverified in the disclosure file (`AI_DISCLOSURE_FILE`); consent to the old text stops working at the very next request (consent is checked against the current verified text on every call, never cached).

## Not set up (the operator's list)

A scheduler for `/v1/maintenance/purge`; an alert delivery channel polling `/v1/alerts`; a ClamAV sidecar; log shipping and retention for the host's logs; a restore test; uptime monitoring from outside; Vercel firewall rules for hard rate limits.
