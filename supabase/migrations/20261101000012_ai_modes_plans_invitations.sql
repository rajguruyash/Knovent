-- 12 · How a business answers (the execution mode and what it may use), what it is entitled to, who is in it, and what was used.
--
--   * ai_inference_settings   one row per business: the mode (deterministic, platform, byok, local), the platform model when the mode is `platform`, and what to do when the chosen AI cannot answer.
--                             Written ONLY by the engine service, which checks the choice against the registry, the catalogue, the plan and the consent; a person's browser cannot write it.
--   * business_plans          what a business is entitled to (trial, free, paid) and its monthly limits. Written only by the platform operator. A business with no row, or a row with a limit left
--                             empty, has no platform inference: nothing is "unlimited" by omission.
--   * usage_records           (migration 06) gains the mode, a correlation id, who generated the answer and why not a model, so that usage can be metered per mode and traced.
--   * local_companions        the companions a business has paired (no secret is stored here: the pairing secret never leaves the person's machine and browser).
--   * business_invitations    a person is added to a business by accepting an invitation sent to their confirmed address, not by being inserted: direct inserts into business_members are closed.
-- Nothing here turns inference on, stores a credential, or lets one business read another's rows.

-- ---------------------------------------------------------------- inference settings
create table public.ai_inference_settings (
  business_id uuid primary key references public.businesses (id) on delete cascade,
  mode text not null default 'deterministic' check (mode in ('deterministic', 'platform', 'byok', 'local')),
  platform_provider text check (platform_provider is null or platform_provider ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
  platform_model text check (platform_model is null or char_length(platform_model) between 1 and 200),
  fallback text not null default 'deterministic' check (fallback in ('deterministic', 'none')),   -- when the chosen AI cannot answer: the deterministic answer, or "unavailable"
  updated_by uuid references auth.users (id) on delete set null,
  updated_at timestamptz not null default now(),
  check (mode <> 'platform' or (platform_provider is not null and platform_model is not null)),
  check (mode = 'platform' or (platform_provider is null and platform_model is null))
);
create trigger ai_settings_touch before update on public.ai_inference_settings for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------- plans and limits
create table public.business_plans (
  business_id uuid primary key references public.businesses (id) on delete cascade,
  plan text not null default 'trial' check (plan in ('trial', 'free', 'paid')),
  inference_requests_per_month integer check (inference_requests_per_month is null or inference_requests_per_month >= 0),   -- platform-mode requests that reach a model
  tokens_per_month bigint check (tokens_per_month is null or tokens_per_month >= 0),
  cost_cap_usd_per_month numeric check (cost_cap_usd_per_month is null or cost_cap_usd_per_month >= 0),
  inference_per_minute integer not null default 20 check (inference_per_minute between 1 and 600),                        -- abuse control, for every mode that reaches a provider
  trial_ends_at timestamptz,
  updated_by uuid references auth.users (id) on delete set null,
  updated_at timestamptz not null default now()
);
create trigger business_plans_touch before update on public.business_plans for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------- usage, metered per mode
alter table public.usage_records
  add column mode text check (mode is null or mode in ('deterministic', 'platform', 'byok', 'local')),
  add column correlation_id text check (correlation_id is null or char_length(correlation_id) <= 64),
  add column generated_by text check (generated_by is null or char_length(generated_by) <= 40),
  add column failure_kind text check (failure_kind is null or char_length(failure_kind) <= 80);
create index usage_records_mode_idx on public.usage_records (business_id, kind, mode, created_at desc);

-- ---------------------------------------------------------------- paired companions
create table public.local_companions (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  label text not null check (char_length(label) between 1 and 80),
  platform text check (platform is null or char_length(platform) <= 80),
  runtime text check (runtime is null or runtime in ('ollama', 'llamacpp')),
  model text check (model is null or char_length(model) <= 200),
  paired_by uuid references auth.users (id) on delete set null,
  paired_at timestamptz not null default now(),
  last_seen_at timestamptz,
  revoked_by uuid references auth.users (id) on delete set null,
  revoked_at timestamptz
);
create index local_companions_business_idx on public.local_companions (business_id, paired_at desc);

-- ---------------------------------------------------------------- invitations
create table public.business_invitations (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  email text not null check (email = lower(email) and char_length(email) <= 254),
  role text not null check (role in ('admin', 'editor', 'viewer')),
  token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),   -- the invitation's secret is shown once to the person who made it and kept only as a hash
  invited_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  accepted_by uuid references auth.users (id) on delete set null,
  accepted_at timestamptz,
  revoked_at timestamptz
);
create index business_invitations_business_idx on public.business_invitations (business_id, created_at desc);

create or replace function public.create_invitation(p_business uuid, p_email text, p_role text) returns text
language plpgsql security definer set search_path = '' as $$
declare
  uid uuid := (select auth.uid());
  e text := lower(btrim(p_email));
  tok text;
begin
  if uid is null then
    raise exception 'sign in first' using errcode = '42501';
  end if;
  if not public.has_role(p_business, array['owner', 'admin']) then
    raise exception 'only an owner or admin can invite' using errcode = '42501';
  end if;
  if p_role not in ('admin', 'editor', 'viewer') then
    raise exception 'choose admin, editor or viewer' using errcode = '22023';
  end if;
  if p_role = 'admin' and not public.has_role(p_business, array['owner']) then
    raise exception 'only an owner can invite an admin' using errcode = '42501';
  end if;
  if e !~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$' or char_length(e) > 254 then
    raise exception 'that is not an email address' using errcode = '22023';
  end if;
  if (select count(*) from public.business_invitations i where i.business_id = p_business and i.accepted_at is null and i.revoked_at is null and i.expires_at > now()) >= 50 then
    raise exception 'too many open invitations: withdraw some first' using errcode = '53400';
  end if;
  if exists (select 1 from public.business_members m join auth.users u on u.id = m.user_id where m.business_id = p_business and lower(u.email) = e) then
    raise exception 'that person is already in this business' using errcode = '23505';
  end if;
  update public.business_invitations i set revoked_at = now() where i.business_id = p_business and i.email = e and i.accepted_at is null and i.revoked_at is null;   -- one open invitation per address
  tok := replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', '');
  insert into public.business_invitations (business_id, email, role, token_hash, invited_by, expires_at)
  values (p_business, e, p_role, encode(sha256(convert_to(tok, 'UTF8')), 'hex'), uid, now() + interval '7 days');
  return tok;
end $$;

-- The invited person accepts with the secret. It works only for a signed-in person whose CONFIRMED address is the one invited, once, before it expires; every refusal reads the same, so that the
-- function cannot be used to find out which secrets or addresses exist.
create or replace function public.accept_invitation(p_token text) returns uuid
language plpgsql security definer set search_path = '' as $$
declare
  uid uuid := (select auth.uid());
  inv public.business_invitations;
  mail text;
  confirmed boolean;
begin
  if uid is null then
    raise exception 'sign in first' using errcode = '42501';
  end if;
  select lower(u.email), (to_jsonb(u) ->> 'email_confirmed_at') is not null into mail, confirmed from auth.users u where u.id = uid;
  select * into inv from public.business_invitations i where i.token_hash = encode(sha256(convert_to(coalesce(p_token, ''), 'UTF8')), 'hex') for update;
  if not found or inv.accepted_at is not null or inv.revoked_at is not null or inv.expires_at <= now() or mail is distinct from inv.email or not coalesce(confirmed, false) then
    raise exception 'this invitation cannot be used' using errcode = '42501';
  end if;
  insert into public.business_members (business_id, user_id, role) values (inv.business_id, uid, inv.role) on conflict (business_id, user_id) do nothing;
  update public.business_invitations i set accepted_by = uid, accepted_at = now() where i.id = inv.id;
  return inv.business_id;
end $$;

create or replace function public.revoke_invitation(p_invitation uuid) returns void
language plpgsql security definer set search_path = '' as $$
declare
  b uuid;
begin
  select i.business_id into b from public.business_invitations i where i.id = p_invitation;
  if b is null or not public.has_role(b, array['owner', 'admin']) then
    raise exception 'no such invitation' using errcode = '42501';
  end if;
  update public.business_invitations i set revoked_at = now() where i.id = p_invitation and i.accepted_at is null and i.revoked_at is null;
end $$;

revoke all on function public.create_invitation(uuid, text, text), public.accept_invitation(text), public.revoke_invitation(uuid) from public, anon;
grant execute on function public.create_invitation(uuid, text, text), public.accept_invitation(text), public.revoke_invitation(uuid) to authenticated, service_role;

-- A person is added to a business by accepting an invitation, never by being inserted by someone else: close the direct insert (create_business and accept_invitation run as the database owner).
drop policy members_add on public.business_members;
revoke insert on public.business_members from authenticated;

-- ---------------------------------------------------------------- audit: the new tables, without content (no email addresses, no invitation secrets)
create or replace function public.audit_event() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  o jsonb := case when tg_op = 'INSERT' then '{}'::jsonb else to_jsonb(old) end;
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
  elsif tg_table_name = 'ai_inference_settings' then
    tid := b::text;
    d := jsonb_build_object('op', tg_op, 'mode_before', o ->> 'mode', 'mode_after', n ->> 'mode', 'provider', n ->> 'platform_provider', 'model', n ->> 'platform_model', 'fallback_after', n ->> 'fallback');
  elsif tg_table_name = 'business_plans' then
    tid := b::text;
    d := jsonb_build_object('op', tg_op, 'plan_before', o ->> 'plan', 'plan_after', n ->> 'plan');
  elsif tg_table_name = 'local_companions' then
    tid := coalesce(n ->> 'id', o ->> 'id');
    d := jsonb_build_object('op', tg_op, 'runtime', n ->> 'runtime', 'revoked', (n ->> 'revoked_at') is not null);
  elsif tg_table_name = 'business_invitations' then
    tid := coalesce(n ->> 'id', o ->> 'id');
    d := jsonb_build_object('op', tg_op, 'role', coalesce(n ->> 'role', o ->> 'role'), 'accepted', (n ->> 'accepted_at') is not null, 'revoked', (n ->> 'revoked_at') is not null);
  else
    return coalesce(new, old);
  end if;
  insert into public.audit_logs (business_id, actor_id, action, target_type, target_id, detail)
  values (b, (select auth.uid()), tg_table_name || '.' || lower(tg_op), tg_table_name, tid, d);
  return coalesce(new, old);
end $$;
create trigger audit_ai_settings after insert or update on public.ai_inference_settings for each row when (pg_trigger_depth() = 0) execute function public.audit_event();
create trigger audit_business_plans after insert or update on public.business_plans for each row when (pg_trigger_depth() = 0) execute function public.audit_event();
create trigger audit_local_companions after insert or update on public.local_companions for each row when (pg_trigger_depth() = 0) execute function public.audit_event();
create trigger audit_invitations after insert or update on public.business_invitations for each row when (pg_trigger_depth() = 0) execute function public.audit_event();

-- ---------------------------------------------------------------- row level security and privileges
alter table public.ai_inference_settings enable row level security;
alter table public.business_plans enable row level security;
alter table public.local_companions enable row level security;
alter table public.business_invitations enable row level security;

create policy ai_settings_read on public.ai_inference_settings for select to authenticated using (public.is_member(business_id));
create policy plans_read on public.business_plans for select to authenticated using (public.is_member(business_id));
create policy companions_read on public.local_companions for select to authenticated using (public.is_member(business_id));
create policy invitations_read on public.business_invitations for select to authenticated using (public.has_role(business_id, array['owner', 'admin']));

revoke all on public.ai_inference_settings, public.business_plans, public.local_companions, public.business_invitations from public, anon, authenticated;
grant select on public.ai_inference_settings, public.business_plans, public.local_companions to authenticated;
grant select (id, business_id, email, role, invited_by, created_at, expires_at, accepted_at, revoked_at) on public.business_invitations to authenticated;   -- never the hash

-- the trigger functions this file adds are not callable by anyone; the new helper functions above have explicit grants (migration 11's pattern)
comment on table public.ai_inference_settings is 'The execution mode a business chose. Written only by the engine service after it has checked the choice; deterministic is the default and never needs consent.';
comment on table public.business_plans is 'What a business is entitled to. Written only by the platform operator. No row, or an empty limit, means no platform inference.';
