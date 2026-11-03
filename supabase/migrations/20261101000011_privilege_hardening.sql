-- 11 · Privilege hardening, from the catalog audit made before the first validation on a real Supabase project (docs/staging-validation.md).
--
-- The audit read every grant in the schema as the migrations leave it, with Supabase's default privileges in force (a new table or function in `public` is granted to `anon`, `authenticated` and
-- `service_role`). Row level security guards what a person can read and change; this migration closes the doors that row level security does not guard, and that nothing needs:
--   1. TRUNCATE, TRIGGER and REFERENCES on tables, for the API roles. TRUNCATE is not subject to row level security, so where a connection as `authenticated` exists it would empty a table
--      whatever the policies say (a person's own browser cannot do this through the Data API; a mis-set connection string or a future feature could). Nothing the product does needs them.
--   2. EXECUTE on trigger functions. They run when a trigger fires, not when a person calls them; the grant at create time is all a trigger needs.
--      And EXECUTE on the helper functions that migration 08 meant to close to `anon` (`revoke ... from anon` does not remove what PUBLIC grants): `norm_text`, `is_server`, `path_business`,
--      `match_embeddings`. Signed-in people and the server keep it (a generated column, a policy or a trigger runs them as the person), the anonymous visitor does not.
--   3. The consent table (migration 10) is append-only history: the API roles get exactly the columns they use, and TRUNCATE of it is refused for everyone, the database owner included.
-- Nothing here changes what a signed-in person can do through the application.

-- 1. tables
revoke truncate, trigger, references on all tables in schema public from anon, authenticated;

-- 3. the consent table: only what the product uses
revoke all on public.ai_inference_consents from public, anon, authenticated;
grant select on public.ai_inference_consents to authenticated;
grant insert (business_id, provider, disclosure_version, disclosure_sha256, data_categories) on public.ai_inference_consents to authenticated;   -- id, status, who and when are the database's to set
grant update (status) on public.ai_inference_consents to authenticated;                                                                           -- the only change a person can make is to revoke

create or replace function public.ai_consent_no_truncate() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'consent history cannot be emptied' using errcode = '23514';
end $$;
create trigger ai_consent_no_truncate before truncate on public.ai_inference_consents for each statement execute function public.ai_consent_no_truncate();

-- 2. trigger functions (including the ones above and in earlier migrations): not callable by anyone but the database owner, whose own privilege is untouched
do $$
declare f regprocedure;
begin
  for f in select p.oid::regprocedure from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.prorettype = 'pg_catalog.trigger'::regtype loop
    execute format('revoke all on function %s from public, anon, authenticated, service_role', f);
  end loop;
end $$;

-- 2b. helper functions that were reachable by PUBLIC (and so by `anon`): signed-in people and the server keep EXECUTE, as they had it
do $$
declare f regprocedure;
begin
  for f in select p.oid::regprocedure from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.prorettype <> 'pg_catalog.trigger'::regtype
             and (has_function_privilege('anon', p.oid, 'EXECUTE') or exists (select 1 from aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a where a.grantee = 0 and a.privilege_type = 'EXECUTE')) loop
    execute format('revoke all on function %s from public, anon', f);
    execute format('grant execute on function %s to authenticated, service_role', f);
  end loop;
end $$;
