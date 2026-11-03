# Validating the engine on a real Supabase staging project

**Status (2026-11-02): pending, on purpose.** The owner will deploy the staging environment and configure its credentials; until then nothing here has run against a Supabase project, and no existing project
(in particular no production project and no other product's project) is to be used. Once it is configured, the validation to run is: **migrations 10, 11, 12 and 13 applied, then RLS, database privileges, the consent
functions and tenant isolation checked with `scripts/validate_supabase.py`**, followed by the person's API-level steps below. Nothing in this repository claims that a live Supabase project has validated any migration.

**Credentials.** All of the engine's keys live in ONE protected file, `~/.config/faq-engine/.env` (outside the repository, in a folder only you can enter, mode 600; the repository's `.gitignore` also refuses `.env`, `*.env`, `*.pem`, `*.key`). Use it with `set -a; source ~/.config/faq-engine/.env; set +a`; see what is set, as SET or MISSING and never a value, with `python scripts/env_status.py`. The values live in a protected environment file that the shell is started from; they are never typed into a chat, a command line or a file in the repository, and the script never prints or logs
them (it reports a connection failure by its class and SQLSTATE only; a test proves it quotes nothing). Variables set in Vercel are for the deployed web app: they do not reach this shell by themselves, so the four names
below have to be present in the environment the script is launched from. (`vercel env pull` into a file outside the repository is one way; whether to do that is the owner's decision.) The database connection string is the
engine service's `DATABASE_URL` for staging, not a Vercel web variable.

## The first project that was offered (2026-11-02), and what could and could not be checked
The owner named a Supabase project (ref `cmgkjtwhbwssrojuxmun`) and put its **publishable** key in a protected env file (one variable, no database connection string, no password, no service key).
* **Identity: confirmed by the owner in chat, not verifiable from here.** The Supabase CLI on the development machine is signed in to a different account, whose projects do not include this ref, so the project's
  name and owner could not be read from here. The owner then stated that it is a NEW project created for this NLP intelligence platform (not another product's, and neither of the two known projects). It is
  treated as the engine's staging project. If it is also meant to become production, a separate production project is still recommended: validation and customer data should never share a database.
* **What a publishable key can do:** reach the project's Data API as an anonymous visitor. `scripts/probe_supabase_anon.py` (13 tests) asks, read-only, for every table and function the migrations define
  whether it exists and whether the anonymous role is refused. It created no account, wrote nothing, and never prints the key.
* **Result: the engine's schema is not there.** All 43 tables and callable functions answered "not found" (`PGRST205` / `PGRST202`) while the key itself was accepted (a rejected key answers 401, not 404).
  Migrations 1 to 13 have **not been applied** to that project (or its `public` schema is not exposed). Nothing about migrations 10, 11 or 12, RLS, privileges, consent or isolation has been validated there.
* **What a publishable key cannot do:** apply a migration, or run `scripts/validate_supabase.py`, which needs the project's Postgres connection string (with the database password).
* **To go on, the owner chooses one:** (a) in an account that owns the project, `supabase link --project-ref cmgkjtwhbwssrojuxmun` then `supabase db push` (migrations 1 to 13: 10 depends on 1 to 9), then give me
  the connection string in the protected env file as `STAGING_DATABASE_URL` with `STAGING_PROJECT_REF`, `STAGING_CONFIRM_REF` and `FORBIDDEN_PROJECT_REFS`; or (b) say explicitly that I may apply
  migrations 1 to 13 to that project, and give me the connection string the same way.

## What was found when the audit looked for a staging environment
* The Supabase CLI on the development machine is signed in to an account that holds projects for **other products**, one of them a live production project. None is the engine's staging project, and none holds
  the engine's tables. **No migration was applied to any of them, and none will be**: the engine's tables would sit beside another product's data, and a "pass" there would prove nothing about a clean project.
* Docker is not installed, so `supabase start` (a local copy of the real stack) is not available either.
* So the only database the checks have run on is a local PostgreSQL 18 with a stand-in for Supabase's roles and `auth`/`storage` schemas (`supabase/tests/00_supabase_stand_in.sql`). Supabase runs PostgreSQL 17
  (`supabase/config.toml`); the tests ran on 18. Those differences are what a staging run is for.

## The safest way to get a staging project
1. **A project of its own**, used for nothing else, named so that nobody mistakes it for production (for example `faq-engine-staging`). Not a branch or schema inside another product's project, and not a project that holds any
   customer's data. Check the plan's limit on active projects before creating it (a free plan may allow only a few; a separate organisation or a paid plan is a decision for the owner, and costs are theirs to approve).
2. **The same region and Postgres major version as the real deployment.**
3. **Keys only in a protected local environment file**, never in the repository, a command line or a chat. The names are below.
4. **Throwaway data only.** The checks create their test people and businesses inside one transaction and roll it back; the hosted shadow test (below) uses fictional example businesses.
5. When finished: pause or delete the project. It is not needed between validations.

## Steps
```bash
# 1. link and look (read-only)
supabase link --project-ref <STAGING ref>          # the CLI refuses nothing here: check the ref twice
supabase db push --dry-run                          # lists the migrations that WOULD run: 01 to 13
# 2. apply them to staging only
supabase db push
# 3. validate (reads the catalog, then runs every check in one transaction that is rolled back)
python scripts/validate_supabase.py --json runs/staging-validation/report.json
```
Environment (names only; set them in the protected file you launch your shell from):

| Name | What |
|---|---|
| `STAGING_DATABASE_URL` | the staging project's Postgres connection string (the database owner's) |
| `STAGING_PROJECT_REF` | the staging project's ref, typed once |
| `STAGING_CONFIRM_REF` | the same ref, typed again on purpose: the script refuses if the two differ |
| `FORBIDDEN_PROJECT_REFS` | comma-separated refs of **production and of every other product's project**; the script refuses any of them even if declared |

`scripts/validate_supabase.py` **refuses**: a target whose ref is not the declared one or is forbidden; a database that does not already hold the engine's tables (it applies no migration itself); a database that holds any
table the engine does not own; a local PostgreSQL unless `--allow-local` is given (and then its first line says it is **not** a staging validation). It never prints the connection string. Exit codes: 0 all checks passed, 1 a check failed, 2 the variable is not set, 3 refused (wrong target), 4 could not run (a database error, reported by class only). Its tests
(`tests/test_staging_validator.py`) prove each refusal and prove that thirteen deliberate misconfigurations (a TRUNCATE grant, a trigger function open to `anon`, row level security off, a policy that lets anyone revoke,
a definer function with no `search_path` ...) are each caught by name.

### What the script checks (53 checks)
* **Catalog:** row level security on every table; the anonymous role holds nothing; no API role can TRUNCATE, TRIGGER or REFERENCE a table; no trigger function is callable by PUBLIC or an API role; every
  `SECURITY DEFINER` function pins an empty `search_path` and is owned by the schema owner; no function is executable by PUBLIC or `anon`; the consent table has no column that could hold a credential, exactly the
  grants, policies, indexes, triggers and constraints it should, and `ai_consent_allows` is `SECURITY INVOKER`, pins its path and runs only for signed-in people and the server; the audit function knows the table and
  records no content.
* **Behaviour, as each kind of person** (the role is switched to `authenticated`/`anon` and the person's id put in the request claims, which is what the Data API does): owner and admin can agree and are recorded as the
  agreeing person; editor, viewer, a stranger and another business's owner cannot, cannot forge who agreed, the status or the id, and cannot revoke; members read, outsiders see nothing; the question is answered yes only for
  the exact provider and disclosure version, and no for anything else or another business; an admin's revocation makes the very next answer no and records who and when; a revoked agreement cannot be revived and a new
  one is a new row; nobody, the server's connection included, can delete or empty the history; the server can record an agreement naming who agreed; every grant and revocation is in the audit trail with the
  acting person, which provider and which versions, and nothing else; only owners and admins read the trail.

### What it does **not** check, and the person's steps for it
The script tests the database's rules under the roles. It does not test PostgREST, GoTrue, Storage, the pooler or the web app. After it passes, with **two real accounts** in the staging project:
1. Sign up two people through the staging web app (Vercel preview pointing at staging); create a business each. Confirm each sees only their own.
2. Through the Data API with each person's own token (not the service key), try: reading the other business's `ai_inference_consents` (expect no rows), inserting an agreement for it (expect refusal), inserting one with
   `granted_by` or `status` in the body (expect refusal), `PATCH`ing any column but `status`, `DELETE`, and calling `rpc/ai_consent_allows` for the other business (expect `false`).
3. As a viewer of one business: open Settings, confirm "Only owners and admins can agree or withdraw", and that the buttons are absent.
4. Confirm the anonymous key cannot call `rpc/ai_consent_allows`, `rpc/norm_text`, `rpc/is_server`, `rpc/path_business` or `rpc/match_embeddings` (migration 11 closed those).
5. Run the hosted service against staging with `ENGINE_PIPELINE=shadow` (below) and read the audit log through the owner's token.
Record the dates and results next to the report. Until these are done, say "migrations validated at the database level on staging", not "validated on Supabase".

## Shadow mode against staging
Run the hosted service (the image, or `python -m faqengine.hosted.service`) with the staging `DATABASE_URL` and:
```
ENGINE_PIPELINE=shadow
ENGINE_SHADOW_SAMPLE=0.1        # the share of requests the pipeline also runs on; the existing path answers every request, whatever this is
```
No external model is called by any setting. `GET /v1/metrics` (with the shared secret; `/health` stays open and says nothing about traffic) shows the counters (`shadow_match`, `shadow_mismatch`, `shadow_diff_<kind>`, `shadow_error`, `pipeline_fallback`, `shadow_unrecorded`)
and the median and 95th percentile of both paths' times; it holds no business, person or text. Each recorded difference is a row in `audit_logs`:
```sql
select created_at, detail->'categories' as kinds, detail->'differences' as what, detail->>'legacy_outcome' as existing, detail->>'pipeline_outcome' as pipeline
from public.audit_logs where business_id = '<business>' and action = 'pipeline.shadow_mismatch' order by created_at desc;
```
At most `shadow_records_per_hour` (50) differences per business and hour are written; the rest are counted (`shadow_unrecorded`). Traffic against staging is **synthetic** unless real people use it; `docs/shadow-validation.md`
says which traffic produced its numbers. Real customer traffic has not been through shadow mode.

## What the local run shows (not a staging validation)
`python scripts/validate_supabase.py --allow-local` on the local PostgreSQL with every migration (1–13): 53 passed, 0 failed, and the report's first line says it is not a staging validation. Run before migration 11 it
found four findings that are fixed in migration 11, below.

## What the audit found, and what migration 11 changes
| Finding | Why it matters | Fix |
|---|---|---|
| `TRUNCATE`, `TRIGGER`, `REFERENCES` granted to `anon` and `authenticated` on **every** table (Supabase's default privileges) | TRUNCATE ignores row level security; where a connection as an API role exists it would empty a table whatever the policies say. Through the Data API it is not reachable; a mis-set connection string or a future feature could reach it. | revoked on all tables |
| Trigger functions (`ai_consent_guard`, `audit_event`, `handle_new_user`, others) executable by PUBLIC and the API roles | They run when a trigger fires, not when a person calls them | execute revoked; triggers still fire |
| `norm_text`, `is_server`, `path_business`, `match_embeddings` executable by PUBLIC, so by `anon` (migration 08 revoked from `anon` only, which does not remove PUBLIC's grant) | Reachable as `/rpc/...` by an anonymous visitor; harmless today, not intended | PUBLIC and `anon` revoked; signed-in people and the server keep them (generated columns, policies and triggers run them as the person) |
| The consent table granted `authenticated` every column for insert (`granted_by`, `status`, `id`) and the guard trigger overwrote them | Correct but only by the trigger; a second lock is cheap | column-level grants: only `business_id, provider, disclosure_version, disclosure_sha256, data_categories`; and a trigger that refuses TRUNCATE of the history even for the database owner |

Migration 11 is a new file rather than an edit to 08 or 10 so that what was reviewed stays as reviewed. No migration has been applied to a live project, so the rule against editing applied migrations has not been
broken; from the first live apply it binds.
