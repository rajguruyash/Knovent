# Hosted deployment: Vercel, Supabase and the engine service

## The decision, and why

```
 Browser ──► Vercel (web/)                         Supabase                      Engine service (Python)
             static app + API functions ──────────► Auth: who is this?             one small always-on container
             • checks the person's token            Postgres + RLS: may they,      • loads the embedding model once
               (Supabase /auth/v1/user)              in THIS business?             • keeps each business's index in memory
             • checks their role (RLS, their own    Storage: uploaded files        • same retrieval / evidence / verification /
               token)                                                                response code as the local engine
             • forwards ONLY whitelisted fields ───────────────────────────────────► • re-checks the role in the database
               with a shared secret                                                  • reads and writes with a server credential
                                                                                     • calls the language model, if the business
                                                                                       turned one on (key decrypted per request)
```

* **Vercel** hosts the static app and thin API functions (plain ESM JavaScript, no run-time dependencies). They authenticate and authorise, then call the engine service.
* **Supabase** is the system of record: Auth, Postgres (RLS on every table, pgvector), Storage.
* **The engine service** (`python -m faqengine.hosted.service`, image in `deploy/engine/Dockerfile`) is a separate always-on process.

### Can the Python engine run inside Vercel Functions?

Checked against Vercel's current limits (docs, read 2026-11-01) and measured here:

| Question | Vercel limit | Measured for the engine (macOS arm64; Linux wheels differ) |
|---|---|---|
| Size | 500 MB uncompressed for Python | dependencies about 160 MB (onnxruntime 80, numpy 36, psycopg 17, cryptography 11, tokenizers 10, pypdf 4) plus the model, 22 MB |
| Memory | 2 GB (Hobby, default and maximum), 4 GB (Pro, maximum) | peak RSS 147 MB with the model loaded and 500 passages embedded |
| Load time | none stated | import 0.22 s, model load 0.04 s |
| Latency | duration default 300 s | one question embedded in 0.8 ms; 500 passages in 0.2 s |
| File descriptors | 1,024 shared across concurrent executions | each database connection and each request uses some |

**So the limits do not rule it out.** The engine is a 22 MB int8 ONNX model, no PyTorch. The reasons for a separate service are about behaviour, not capacity:

1. **State.** Each business's index (vectors, keyword statistics) is kept in memory and rebuilt only when its knowledge changes. Function instances are created and discarded; every cold instance would reload every active business's rows from the database. The per-business rate limit and the index cache are in-process.
2. **Database connections.** The engine holds real PostgreSQL connections; many short-lived instances compete for connections and the 1,024-descriptor budget (a pooler is then required).
3. **Work that is not a request.** Reading a web site (robots.txt, politeness delays, up to 10 pages) and embedding a large upload are better as a bounded background job than inside a request.
4. **One implementation of the rules.** The same code as the local engine runs unchanged in the container; no re-packaging for a function runtime.

A Vercel Python function is a feasible alternative for a very small deployment (one `api/*.py` adapter around `HostedEngine`). **It was not built or tested**; do not assume it works.

The container image runs on any always-on host (Render, Fly.io, Railway, Cloud Run with minimum one instance, a VM). It was **not built here** (no Docker was available): treat the first build as part of deployment and run `tests/test_hosted.py`'s scenarios against it.

## What is local and what is hosted

| | Local mode (Studio / exported package) | Hosted mode |
|---|---|---|
| Runs on | the user's computer | Vercel + Supabase + the engine service |
| Needs internet to answer | **No.** Proven by `tests/test_offline.py` (every network connection forbidden; the complete answering workflow still runs) | yes |
| Needs Supabase | **No** | yes |
| Needs a language-model key | **No** | **No** (optional per business) |
| Embedding model | the project's own, possibly fine-tuned | the shared pretrained all-MiniLM-L6-v2 (int8). **No fine-tuning happens in hosted mode.** |
| Training and held-out evaluation | yes (Studio) | not in hosted mode |

The local-only capability is provided by the Python engine on the user's machine; it is not provided by Vercel or Supabase.

## Environment variables

**Vercel project (`web/`)**

| Name | Secret? | Meaning |
|---|---|---|
| `SUPABASE_URL` | no | `https://<project>.supabase.co` |
| `SUPABASE_ANON_KEY` | no (public by design; safe because RLS guards the data) | the project's anon key |
| `ENGINE_URL` | yes (server only) | address of the engine service |
| `ENGINE_SERVICE_SECRET` | **yes** | shared secret the functions send to the engine (at least 24 random characters) |

No service-role key and no database password belong in Vercel. `GET /api/config` returns only `SUPABASE_URL` and `SUPABASE_ANON_KEY`; a test scans everything served to a browser for secrets.

**Engine service**

| Name | Meaning |
|---|---|
| `DATABASE_URL` | Postgres connection string with a server-side role (use Supabase's pooled connection string) |
| `ENGINE_SERVICE_SECRET` | the same shared secret (at least 24 characters) |
| `EMBEDDING_MODEL_DIR` | folder from `scripts/build_base_model.py` (the image sets it) |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | to read uploaded files from Storage (service-role key: server only) |
| `LLM_CREDENTIAL_KEYS` | `2:<base64 of 32 random bytes>,1:<older>` — encrypts businesses' API keys. Without it the service refuses to store a key. Generate: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` |
| `RATE_LIMIT_PER_MINUTE` | questions per business per minute (default 60) |
| `ENGINE_ALLOW_PRIVATE_LLM` | `1` only for tests/local setups: lets a business point at a model server on a private address. Leave unset in production. |
| `ENGINE_PIPELINE` | `off` (default), `shadow` or `on`: how the reasoning pipeline is used (`docs/reasoning-pipeline.md`). Roll out as `shadow` (the existing path answers, the pipeline is compared and differences land in `audit_logs` as `pipeline.shadow_mismatch`; counters in `GET /v1/metrics` with the shared secret), then `on`; set back to `off` and restart to roll back. Any other value stops the service at start. No setting calls an external model. |
| `ENGINE_SHADOW_SAMPLE` | `0` to `1` (default `1`): the share of requests the pipeline is also run on when `ENGINE_PIPELINE=shadow`. Measured: running it inline roughly doubles a sampled request's time on the test machine (`docs/shadow-validation.md`), so use a small share for a live test. |
| `ENGINE_FAST_PATH` | `on` (default) or `off`: whether a question the engine already answers completely and verifiably (an exact FAQ entry, a calculation worked out in code) skips the model (`docs/fast-path.md`). It never enables, disables or reorders any AI mode and changes no consent or setting; `off` restores the earlier behaviour, where every answer in an AI mode goes to a model. Any other value stops the service at start. |
| `AI_DISCLOSURE_FILE` | optional JSON file with the per-provider disclosure details (terms address, the date they were read, retention, training use). Empty by default, and the text a business reads then says the terms are not verified. Editing it is a new disclosure version: earlier agreements stop applying (`docs/ai-disclosure.md`). |
| `ENGINE_AI_MODES` | **the operator's kill switch for AI inference.** Comma-separated subset of `platform`, `byok`, `local`; **empty (the default) means no business can use any AI mode, whatever it has configured or agreed to** — deterministic answering always works. Anything else stops the service at start. |
| `PLATFORM_CLOUDFLARE_ACCOUNT_ID`, `PLATFORM_CLOUDFLARE_API_TOKEN`, `PLATFORM_OPENROUTER_API_KEY` | the platform's own provider credentials for `platform` mode. **Secrets**, held only in the engine's environment; reported by `/v1/metrics` as SET/MISSING, never as values. Deliberately not the `CLOUDFLARE_*`/`OPENROUTER_*` names the benchmark uses. A model is also not usable until its catalogue entry is `platform_offered`, its provider's terms are verified, and the business has agreed. |
| `ENGINE_INFERENCE_PER_MINUTE` | default abuse limit per business for every mode that reaches a provider (default 20; a business's plan can set 1–600) |
| `ENGINE_PLATFORM_CONCURRENCY` | platform calls in flight at once, shared by all businesses (default 8) |
| `MODEL_CATALOGUE_FILE` | optional replacement for `inference/catalogue.json` (the models the platform could offer and what is known of each) |
| `ENGINE_SCANNER`, `CLAMAV_HOST`, `CLAMAV_PORT`, `ENGINE_SCAN_REQUIRED` | upload malware scanning through ClamAV's `INSTREAM` (`ENGINE_SCANNER=clamav` needs `CLAMAV_HOST`; port default 3310). With `ENGINE_SCAN_REQUIRED=1` an unreachable scanner refuses uploads (503); otherwise the file is read and the result says `scanned: false`. |
| `ENGINE_LOG_LEVEL`, `ENGINE_LOG_FORMAT` | `INFO` and `json` by default; logs carry correlation ids and counts, never keys, questions or answers (`docs/operations.md`) |
| `PORT` | default 8090 |

**Scheduled jobs (not set up).** `POST /v1/maintenance/purge` with the shared secret deletes messages past each business's retention; call it once a day from a scheduler (Vercel Cron, or the host's). Poll `GET /v1/alerts` (shared secret) from a monitor. `docs/operations.md` has the alert table and the runbooks.

Keep the service on a private network or restrict it to Vercel's egress where your host allows; the shared secret is the second lock, not the only one. Rotate `ENGINE_SERVICE_SECRET` by deploying both sides.

## Deployment steps

0. **Staging first.** Apply and validate everything on a separate staging project before any other (`docs/staging-validation.md`: `scripts/validate_supabase.py` after `supabase db push`). Nothing here has been validated on a live Supabase project yet.
1. **Supabase.** Create a project. Apply `supabase/migrations/*.sql` (13 files) in order (`supabase link` then `supabase db push`, or paste each into the SQL editor). Enable e-mail sign-in under Authentication **with e-mail confirmation on**: an invitation can be accepted only by a person whose address is confirmed. Check that the `knowledge-files` bucket exists and is private.
2. **Engine model.** `python scripts/build_base_model.py` (writes `hosted_models/minilm-l6`).
3. **Engine service.** Build `deploy/engine/Dockerfile` from the repository root, set the engine variables, deploy, and check `GET /health`.
4. **Vercel.** Import the repository, set the project's Root Directory to `web`, set the four variables, deploy. `npm run build` copies the Supabase client into `public/vendor`.
5. **Smoke test** (Section below), then invite people.

### Smoke test after deploying (not yet run: no deployment exists)

Sign up; create a business; upload a small FAQ file; press read; ask a question you know the answer to and one you know it does not cover; check the second says so. Sign in as a second user in a second business and confirm they cannot see the first. In the Network tab confirm no response contains a key.

## Failure handling

| Failure | What the person sees |
|---|---|
| Not signed in / session expired | 401, "Sign in again." |
| Wrong business or role | 403 (the same message whether the business exists or not) |
| Engine service down or slow | 502/504, "The knowledge engine is not available right now." (secret, address and trace are never returned) |
| Too many questions | 429 with `Retry-After` (per business in the engine, per person in the function) |
| Language model unavailable, invalid key, rate limited | the answer is still given in local-only wording; the result says the model was tried and why it was not used |
| A model is switched on for a provider whose terms are not verified, or the business has not agreed | the model is not used and no data is sent; Settings says "Not in use" and why; answers stay local |
| Agreeing to a provider that is unverified, or to a text that changed since it was shown | 409 with the reason; nothing is recorded |
| A source cannot be read | the source is marked failed with a plain message; the previous good reading stays searched |

The in-function rate limit is best effort (per instance memory). For a real limit add a Vercel Firewall rate-limit rule on `/api/*`.

## The consent functions
`/api/ai/disclosure`, `/api/ai/consent/get`, `/api/ai/consent/grant`, `/api/ai/consent/revoke` (`web/api/ai/`) are built with the same `route()` as every other function: sign-in checked with Supabase, role checked in this business with the
person's own token, only whitelisted fields forwarded (the provider and, for an agreement, the disclosure version that was shown), the verified user id supplied by the function, never by the browser. Any member reads;
owners and admins agree and withdraw. The engine service decides whether a provider can be agreed to (verified terms), whether the version is the current text, and records who agreed.

## Rollback

* **Pipeline:** set `ENGINE_PIPELINE=off` and restart the engine service (an unrecognised value stops it at start; a fault inside the pipeline already falls back to the existing path by itself).
* **Consent:** an owner or admin withdraws in Settings, or the entry for a provider is edited so that it is unverified: either stops the next call. The agreement history is never deleted.
* **Web:** promote a previous Vercel deployment.
* **Engine:** redeploy the previous image; it is stateless apart from caches.
* **Knowledge:** `knowledge_versions` and `rollback_to_version()` (SQL, tested); a source's previous reading is kept in `knowledge_source_versions` (summary) and its chunks stay in the table, inactive.
* **Schema:** write a new migration that undoes the change; do not edit applied migrations.

## Not verified here

Vercel build and deploy (no Vercel account or CLI); preview deployments; cold-start behaviour of the web functions; Supabase live project; the container image; the engine on Linux; behaviour under load. Each is listed again in `docs/production-readiness.md`.
