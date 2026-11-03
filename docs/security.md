# Security and privacy review

What is protected, how, what each claim rests on, and what has **not** been verified. "Tested" means an automated test in this repository fails if the protection is removed. Passing tests is not a security audit: no independent review, penetration test or live deployment exists, and the list at the end is the honest remainder.

Reviewed on 2026-11-02: every migration (1–13), every SQL function and trigger, every `SECURITY DEFINER` function, the grants and default privileges, the Vercel functions, the engine service's routes, the provider adapters and the local companion.

## Threats and measures

| Threat | Measure | Evidence |
|---|---|---|
| One business reading or changing another's data | `business_id` on every tenant table; composite foreign keys so a row cannot point at another business's parent; row-level security on every table; the service re-checks the caller's role in the database on every request | **Tested** on real PostgreSQL (`test_supabase_rls.py`, `test_hosted.py::test_one_business_never_sees_anothers_knowledge_or_answers_for_it`, `test_unified_hosted.py`: zero sources, candidates or answers of another business over all 98 questions). **Not verified** on a Supabase project |
| A browser claiming a business, user, role, provider or model | the function reads the person's role with **their own token** (RLS answers), replaces `user_id` with the verified one, forwards only whitelisted fields; the engine checks again; provider/model for a call come only from stored settings | **Tested** (`web/tests/api.test.js`; `test_ai_modes.py`: the router takes nothing from the request) |
| Selecting an unauthorised provider or model | platform mode accepts only a model with `platform_offered: true`; BYOK only a saved provider that supports BYOK; saving anything else is refused (409) | **Tested** (`test_ai_modes.py::test_only_a_model_released_on_purpose_can_be_chosen_for_platform_mode`) |
| Sending a business's text to a provider without agreement | per call, on the server: the provider's terms must be **verified**, the business must have agreed to **that provider** and **that disclosure version**; checked in the database each time, never cached; the BYOK key is not even decrypted before this passes | **Tested** (`test_ai_consent.py`, `test_hosted_pipeline.py`, `test_ai_modes.py`) |
| Reusing another business's, another provider's or an older text's agreement | the agreement row is per business, provider and disclosure version (a changed text needs a new agreement); an owner/admin only can agree or withdraw | **Tested** (`test_ai_consent.py::test_consent_to_one_provider_is_not_consent_to_another…`, `test_hosted_pipeline.py::test_one_business_agreeing_does_not_open_another_business_to_the_provider`) |
| Continuing to send after a withdrawal | the next request after a withdrawal is refused (`consent:missing_or_revoked`); a revoked row can never be revived; the history cannot be deleted even by the database owner (trigger) | **Tested** |
| An unverified provider being offered, agreed to or called | the disclosure entry has to carry every item of the verification (terms URL, tier, retention, training use, abuse monitoring, commitments, date, who); until then it is neither shown for agreement nor used; there are **no** verified entries | **Tested** (`test_ai_disclosure.py`, 27) |
| Bypassing usage limits | limits are read from the database on the server before the call; a plan with a missing limit allows no platform inference; a customer has no write path to its own plan | **Tested** (`test_ai_modes.py`, `test_ai_modes_schema.py`) |
| Fabricated citations / invented facts from a model | a claim must cite an evidence id the service issued; every number and name must appear in the cited evidence; a failing claim is dropped and the answer is **not** marked verified; evidence for a local reply is the evidence the service held, not what the client says | **Tested** (`test_hosted_pipeline.py`, `test_ai_modes.py::test_a_model_that_invents_evidence_or_facts_never_gets_through_in_any_mode`, `test_verify.py`) |
| API keys exposed | never returned by any endpoint, log line or error (provider messages are redacted of the key); hosted keys encrypted with AES-256-GCM, the business id and key version bound as authenticated data (a copied ciphertext does not decrypt for another business); the encryption key only in the service's environment; platform credentials only in the service's environment, reported as SET/MISSING | **Tested** (`test_hosted.py::test_a_key_copied_to_another_business_is_unusable…`, `test_inference.py`, `test_telemetry.py`). Local-Studio mode stores a key unencrypted in a 0600 file (stated in the interface) |
| Secrets in the browser | the page gets only the project URL and the public anon key; a test scans everything served for secret names and key shapes; strict CSP (no inline scripts, no `unsafe-eval`, `frame-ancestors 'none'`, `object-src 'none'`, `connect-src` limited to self, Supabase and the loopback companion) | **Tested** (`web/tests/static.test.js`, `web/tests/security.test.js`) |
| SSRF through a crawled URL | the connected address is checked (a redirect or DNS trick fails); private, loopback, link-local refused; robots.txt obeyed; size, time and page limits | **Tested** (`test_web.py`) |
| SSRF through a provider address | a hosted business can choose only a fixed provider (the typed address is ignored) or an **https** server on a **public** address; the name is resolved **once** and that address is connected to (no second lookup to rebind); IPv4/IPv6/mapped/link-local/multicast/CGNAT/`0.0.0.0` are refused; redirects are never followed; the platform credential is only ever sent to the fixed provider address | **Tested** (`test_ssrf.py`, 48; `test_llm.py`; `test_hosted.py`) |
| CORS / CSRF | the API is called with a bearer token in a header, never a cookie, so a foreign page cannot make a visitor's browser act. No response carries any `Access-Control-*` header; a preflight is answered 405; a form post, plain text or multipart body is refused (415) before anything is read; a request carrying only cookies is not signed in | **Tested** (`web/tests/security.test.js`) |
| A local web page (or DNS rebinding) talking to the companion | loopback only; signed requests (HMAC, timestamp ±60 s, nonce); origin bound to the pairing; `Host` check; one-use pairing code burned after five wrong guesses; non-extractable key in the browser | **Tested** (`test_companion.py`, `web/tests/companion.test.js`) |
| Prompt injection in uploaded documents and web pages | text addressed to an AI is flagged when a source is read; flagged passages are quarantined (never searched, never sent to a model); prompts fence data in tags with random names; **and whatever a model returns is checked against the evidence**, so an obeyed instruction (a link, a price, a promise) fails verification | **Tested** (`test_llm_answering.py`, `test_hosted.py`, `scripts/llm_evaluation.py`). The pattern list is heuristic; verification is the real defence |
| Uploading malware or oversized files | extension allow-list, bucket size limit and MIME allow-list (Storage configuration), 200 MB total unpacked budget per file and per-part limits for archives and workbooks, bodies capped (256 KB at the function, 1 MB at the engine); documents are parsed as text, never executed; a **ClamAV `INSTREAM` hook** refuses an infected file and, when `ENGINE_SCAN_REQUIRED=1`, refuses uploads while the scanner is unreachable | **Tested** against a stand-in scanner (`test_hardening.py`). **No scanner is deployed**; Storage limits are a live-project setting (**not verified**) |
| Evidence sent outside the business without the business's say-so | **Local first**: outside steps (own provider account, free hosted, paid hosted) are all **off by default**, each can be turned on only after the business has agreed to that provider's verified terms (refused at save, checked again at every request), the paid step also needs a spending cap, and nothing a failure, the browser or the service does can add a step; a cancel or a bad model answer does not hand the evidence to another provider | **Tested** (`test_local_first.py`, 23: what each provider received is read from its side) |
| A key or password file ending up as knowledge, or leaking from a dropped `.env` | a `.env`, key, certificate or credentials file is refused as knowledge by the page and by the engine; a dropped `.env` is parsed in the browser for five provider-key names only, nothing else is read, the file is not uploaded, the key is shown by its last four characters and encrypted on the server | **Tested** (`web/tests/local_first_ui.test.js`, `test_hardening.py`) |
| Joining a business you were not invited to | direct inserts into `business_members` are closed; a person joins only by accepting an invitation sent to their **confirmed** e-mail address (secret shown once, stored as a hash, expires, single use); an admin cannot create an admin | **Tested** (`test_ai_modes_schema.py`, `test_e2e.py`) |
| Abuse and floods | per-minute limit per business for every mode that reaches a provider (default 20, plan value 1–600); per-person per-endpoint limits in the functions (best effort, per instance); platform concurrency cap; connection cap; circuit breakers | engine limits **tested**; the function limit is per instance, so a real limit needs Vercel's firewall (not configured) |
| Tampering with the audit trail | append-only, sensitive changes audited without secrets or content; consent and mode changes name the acting person | **Tested** |
| Data that must be deletable | `delete_business_data()` cascades to everything including the key and leaves a content-free receipt; **retention is enforced by `POST /v1/maintenance/purge`** (per business, bounded 1–3650 days) | **Tested**; a scheduler must call it (**not set up**) |
| Privilege through the database's defaults | migration 11 revokes `TRUNCATE/TRIGGER/REFERENCES` from API roles on every table (TRUNCATE ignores RLS), PUBLIC/`anon` execute on helper and trigger functions, and narrows the consent table to column grants; every `SECURITY DEFINER` function pins `search_path = ''` | **Tested** locally; the same checks in `scripts/validate_supabase.py` (53) are what a staging run executes |

## Cross-tenant attack tests

Two independent businesses, each attack attempted by the other's owner or by a member of the same business with too low a role, on the real schema:

| Attempt | Result | Test |
|---|---|---|
| read another business's documents or FAQ rows | none visible; service answers 403 | `test_supabase_rls.py`, `test_hosted.py` |
| read another business's evidence (via questions about its facts) | zero leaked sources/candidates/answers over 98 questions; a pipeline run that picked up another tenant's evidence is a recorded failure | `test_unified_hosted.py`, `test_hosted_pipeline.py::test_evidence_from_another_business_is_a_recorded_failure_of_parity` |
| read or use another business's provider key | RLS hides the row; a ciphertext copied across does not decrypt | `test_supabase_rls.py`, `test_hosted.py` |
| reuse another business's consent; agree on its behalf | no rows; refused | `test_ai_consent.py::test_another_business_cannot_see_or_change_this_ones_consent`, `test_hosted_pipeline.py::test_tenant_a_cannot_read_or_change_tenant_bs_consent` |
| consume another business's plan/usage | one business exhausting its plan does not touch another | `test_ai_modes.py::test_one_business_exhausting_its_plan_does_not_touch_another_business` |
| complete another person's / business's local request | 403 (`security_denied` counted); a request id works once | `test_ai_modes.py` |
| pose as another provider after a failure | a failed provider is never replaced by another | `test_hosted_pipeline.py::test_a_failed_provider_is_never_replaced_by_another_one` |
| join a business without an invitation, or with another person's | refused | `test_ai_modes_schema.py`, `test_e2e.py` |

## Retrieved text is untrusted

A document is written by someone the engine does not control. It can inform an answer; it can never instruct the engine. It is searched, quoted with a reference, and, if a model is used, placed in a fenced block as data. Nothing it says changes a rule, a role, a prompt or a setting.

## Privacy

* **Deterministic mode** sends nothing anywhere. It is the default and the fallback for everything.
* **With a model** (platform or BYOK), the question and the evidence passages found for it go to the chosen provider, after the business agreed to that provider's verified disclosure; nothing else is sent (not the rest of the documents, other customers' questions or the key owner's details). **Local mode** sends nothing to any provider.
* **Hosted conversations** are stored unless the business sets `privacy.log_questions` to false, for at most `privacy.retention_days`. Customers are identified only by an opaque reference.
* **Usage records** hold counts, times, status, the mode and provider names and a cost estimate, never content.
* **Logs** carry correlation ids and counts, never keys, questions, evidence or answers.

## What was found in this review and fixed

Migration 11 (privilege hardening, four findings, `docs/staging-validation.md`); invitations replace direct membership inserts (migration 12); a total unpacking budget for workbooks (the per-part limit alone let many sheets add up); a cap on database connections per process; the missing retention job; the missing upload scanning hook; the pipeline fallback label `deterministic_no_consent` so a refusal is not mistaken for a provider failure.

## Not verified, and not claimed

* **No live Supabase, Vercel or container deployment exists.** Everything database-level is tested on a local PostgreSQL with a stand-in for Supabase's roles; the real Data API, Auth, Storage, the pooler and Vercel's headers have not been exercised. `scripts/probe_supabase_anon.py` ran once, read-only, against the project the owner declared for this work: it showed the anonymous key reaches the API and finds none of the engine's tables, i.e. **migrations are not applied there**.
* No independent security review, penetration test, threat-modelling workshop or dependency/vulnerability scan has been done; no SAST is configured.
* The injection patterns are heuristic and will miss novel phrasing.
* ClamAV is a hook, not a deployed scanner; Storage MIME/size limits are configuration of a project that does not exist yet.
* Local-Studio mode keeps a model key in a protected plain-text file.
* The function-level rate limit is per instance.
* Retention enforcement needs the scheduler; a database provider's backups outlive a deletion until they expire.
* Companion security is tested against stand-in runtimes on macOS only (`docs/local-companion.md`).
