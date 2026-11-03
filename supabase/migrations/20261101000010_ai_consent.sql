-- 10 · Consent to AI inference: which business has agreed, for which provider, under which disclosure, and when it stopped.
--
-- Nothing in this migration turns inference on, and nothing in it stores a credential. It records a business's explicit, revocable agreement that the QUESTION and the EVIDENCE PASSAGES retrieved for it
-- may be sent to one named third-party provider, under one version of the disclosure it was shown (docs/ai-disclosure.md). The service checks it on the server before every external call
-- (public.ai_consent_allows, or the same query from the service); a front end's check is never the check. Deterministic mode never reads it because it never sends anything.
--   * one row per grant, kept for good: revoking changes that row (granted -> revoked, with who and when) and a new grant is a new row, so the history of what was agreed and when is the table itself;
--   * consent to one provider says nothing about another, and consent to an older disclosure version does not carry over to a newer one (the service compares versions);
--   * only an owner or admin of the business can grant or revoke; a member cannot, and no one can edit a grant (only revoke it) or delete one.

create table public.ai_inference_consents (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  provider text not null check (provider ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),                    -- a provider key (cloudflare, openrouter, gemini ...): never a key, never a URL
  status text not null default 'granted' check (status in ('granted', 'revoked')),
  data_categories text[] not null default '{question,evidence_passages}' check (cardinality(data_categories) between 1 and 8),   -- what may be sent, as the disclosure listed it
  disclosure_version text not null check (char_length(disclosure_version) between 1 and 80),
  disclosure_sha256 text check (disclosure_sha256 is null or disclosure_sha256 ~ '^[0-9a-f]{64}$'),   -- the text shown, fingerprinted: "version 3" can be checked to mean what was displayed
  granted_by uuid default auth.uid() references auth.users (id) on delete set null,
  granted_at timestamptz not null default now(),
  revoked_by uuid references auth.users (id) on delete set null,
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((status = 'granted') = (revoked_at is null))
);
create unique index ai_consent_one_active_grant on public.ai_inference_consents (business_id, provider) where status = 'granted';
create index ai_consent_business_idx on public.ai_inference_consents (business_id, provider, granted_at desc);
create trigger ai_consent_touch before update on public.ai_inference_consents for each row execute function public.touch_updated_at();

-- What can happen to a row: it is created as a grant by the person acting (or by the server, naming who), and a grant can be revoked. Nothing else: no edit, no un-revoke, no delete (the cascade of
-- deleting the whole business is the only thing that removes rows).
create or replace function public.ai_consent_guard() returns trigger
language plpgsql set search_path = '' as $$
declare
  by_server boolean := coalesce((select auth.role()), '') = 'service_role' or current_user = 'postgres' or (select auth.uid()) is null;
begin
  if tg_op = 'INSERT' then
    if new.status <> 'granted' or new.revoked_at is not null then
      raise exception 'a consent is created as a grant' using errcode = '23514';
    end if;
    if not by_server then
      new.granted_by := (select auth.uid());   -- the person acting, whatever the request said
    end if;
    new.granted_at := now();
    return new;
  elsif tg_op = 'UPDATE' then
    if old.status = 'granted' and new.status = 'revoked'
       and (to_jsonb(new) - 'status' - 'revoked_at' - 'revoked_by' - 'updated_at') = (to_jsonb(old) - 'status' - 'revoked_at' - 'revoked_by' - 'updated_at') then
      new.revoked_at := now();
      if not by_server then
        new.revoked_by := (select auth.uid());
      end if;
      return new;
    end if;
    raise exception 'a consent can only be revoked: it cannot be edited or granted again (grant anew instead)' using errcode = '23514';
  else
    if pg_trigger_depth() > 1 then   -- the business itself is being deleted
      return old;
    end if;
    raise exception 'consent history cannot be deleted' using errcode = '23514';
  end if;
end $$;
create trigger ai_consent_guard before insert or update or delete on public.ai_inference_consents for each row execute function public.ai_consent_guard();

-- The question the service asks before every external call: is there an active grant for this provider under this disclosure version?
create or replace function public.ai_consent_allows(p_business uuid, p_provider text, p_version text) returns boolean
language sql stable security invoker set search_path = '' as $$
  select exists (select 1 from public.ai_inference_consents c
                 where c.business_id = p_business and c.provider = p_provider and c.status = 'granted' and c.disclosure_version = p_version)
$$;
revoke all on function public.ai_consent_allows(uuid, text, text) from public, anon;
grant execute on function public.ai_consent_allows(uuid, text, text) to authenticated, service_role;

-- the audit trail learns about consent (grant, revoke): who, which provider, which disclosure version; never any content
create or replace function public.audit_event() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  o jsonb := case when tg_op = 'INSERT' then '{}'::jsonb else to_jsonb(old) end;   -- OLD does not exist for an INSERT, NEW not for a DELETE
  n jsonb := case when tg_op = 'DELETE' then '{}'::jsonb else to_jsonb(new) end;
  b uuid := coalesce(n ->> 'business_id', o ->> 'business_id')::uuid;
  d jsonb;
  tid text;
begin
  if tg_table_name = 'business_members' then
    tid := coalesce(n ->> 'user_id', o ->> 'user_id');
    d := jsonb_build_object('op', tg_op, 'role_before', o ->> 'role', 'role_after', n ->> 'role');
  elsif tg_table_name = 'llm_provider_configs' then
    tid := b::text;
    d := jsonb_build_object('op', tg_op, 'provider', coalesce(n ->> 'provider', o ->> 'provider'), 'model', coalesce(n ->> 'model', o ->> 'model'), 'enabled_before', o ->> 'enabled', 'enabled_after', n ->> 'enabled');
  elsif tg_table_name = 'knowledge_sources' then
    tid := o ->> 'id';
    d := jsonb_build_object('op', tg_op, 'name', o ->> 'name', 'type', o ->> 'source_type', 'version', o ->> 'version');
  elsif tg_table_name = 'evaluation_datasets' then
    tid := n ->> 'id';
    d := jsonb_build_object('op', tg_op, 'name', n ->> 'name', 'role', n ->> 'role', 'sealed', (n ->> 'sealed_at') is not null);
  elsif tg_table_name = 'knowledge_versions' then
    tid := n ->> 'id';
    d := jsonb_build_object('op', tg_op, 'version', n ->> 'version', 'status_before', o ->> 'status', 'status_after', n ->> 'status');
  elsif tg_table_name = 'ai_inference_consents' then
    tid := coalesce(n ->> 'id', o ->> 'id');
    d := jsonb_build_object('op', tg_op, 'provider', coalesce(n ->> 'provider', o ->> 'provider'), 'status_before', o ->> 'status', 'status_after', n ->> 'status',
                            'disclosure_version', coalesce(n ->> 'disclosure_version', o ->> 'disclosure_version'));
  else
    return coalesce(new, old);
  end if;
  insert into public.audit_logs (business_id, actor_id, action, target_type, target_id, detail)
  values (b, (select auth.uid()), tg_table_name || '.' || lower(tg_op), tg_table_name, tid, d);
  return coalesce(new, old);
end $$;
create trigger audit_ai_consent after insert or update on public.ai_inference_consents for each row when (pg_trigger_depth() = 0) execute function public.audit_event();

-- ---- row level security and privileges
alter table public.ai_inference_consents enable row level security;
create policy ai_consent_read on public.ai_inference_consents for select to authenticated using (public.is_member(business_id));
create policy ai_consent_grant on public.ai_inference_consents for insert to authenticated with check (public.has_role(business_id, array['owner', 'admin']));
create policy ai_consent_revoke on public.ai_inference_consents for update to authenticated
  using (public.has_role(business_id, array['owner', 'admin'])) with check (public.has_role(business_id, array['owner', 'admin']));
revoke all on public.ai_inference_consents from anon;
revoke update, delete on public.ai_inference_consents from authenticated;
grant update (status) on public.ai_inference_consents to authenticated;   -- the only change a person can make is to revoke (the guard sets who and when)
comment on table public.ai_inference_consents is 'A business''s revocable agreement that the question and retrieved evidence passages may be sent to one named third-party provider, under one disclosure version. No credentials.';
