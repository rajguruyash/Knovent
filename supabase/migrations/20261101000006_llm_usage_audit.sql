-- 06 · Language-model configuration (one per business), usage, the audit trail, and deleting a business's data.

-- What a person may see and change. The key itself is NOT here.
create table public.llm_provider_configs (
  business_id uuid primary key references public.businesses (id) on delete cascade,
  provider text not null check (provider in ('openai', 'anthropic', 'gemini', 'openai_compatible')),
  model text not null default '',
  base_url text,
  enabled boolean not null default false,
  capabilities jsonb not null default '{"polish": true, "grounded": true, "assist": true}'::jsonb,
  has_key boolean not null default false,                       -- set by the server when it stores a key: a person cannot claim there is one
  key_validated_at timestamptz,
  key_status text not null default 'unchecked' check (key_status in ('unchecked', 'valid', 'invalid')),
  updated_by uuid references auth.users (id) on delete set null,
  updated_at timestamptz not null default now()
);
create trigger llm_provider_configs_touch before update on public.llm_provider_configs for each row execute function public.touch_updated_at();

-- The key, encrypted by the server (AES-256-GCM, with a key that is not in the database). Row level security is on and there is no policy: only the server reaches it.
create table public.llm_provider_secrets (
  business_id uuid primary key references public.businesses (id) on delete cascade,
  encrypted_api_key bytea not null,
  key_version integer not null default 1,
  updated_at timestamptz not null default now()
);

create table public.usage_records (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  kind text not null check (kind in ('question', 'embedding', 'llm_call', 'ingest', 'crawl')),
  message_id uuid,
  provider text,
  model text,
  tokens_in integer,
  tokens_out integer,
  cost numeric,                                                  -- null unless a price was configured: never guessed
  latency_ms integer,
  status text not null default 'ok' check (status in ('ok', 'failed', 'fallback')),
  created_at timestamptz not null default now()
);
create index usage_records_business_idx on public.usage_records (business_id, created_at desc);

create table public.audit_logs (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  actor_id uuid,
  action text not null,
  target_type text not null,
  target_id text,
  detail jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index audit_logs_business_idx on public.audit_logs (business_id, created_at desc);

-- the audit trail is append-only; only the deletion of the whole business (a cascade) removes rows
create or replace function public.audit_append_only() returns trigger
language plpgsql set search_path = '' as $$
begin
  if pg_trigger_depth() > 1 then
    return coalesce(old, new);
  end if;
  raise exception 'the audit log cannot be changed or deleted' using errcode = '23514';
end $$;
create trigger audit_append_only before update or delete on public.audit_logs for each row execute function public.audit_append_only();

-- (a trigger's WHEN is evaluated one level of nesting before its function runs: 0 here means "a direct statement", not a cascade from deleting the business)
-- one function writes audit rows; triggers on the sensitive tables call it. Secrets never appear in `detail`.
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
  else
    return coalesce(new, old);
  end if;
  insert into public.audit_logs (business_id, actor_id, action, target_type, target_id, detail)
  values (b, (select auth.uid()), tg_table_name || '.' || lower(tg_op), tg_table_name, tid, d);
  return coalesce(new, old);
end $$;
create trigger audit_members after insert or update or delete on public.business_members for each row when (pg_trigger_depth() = 0) execute function public.audit_event();
create trigger audit_llm_config after insert or update or delete on public.llm_provider_configs for each row when (pg_trigger_depth() = 0) execute function public.audit_event();
create trigger audit_source_delete after delete on public.knowledge_sources for each row when (pg_trigger_depth() = 0) execute function public.audit_event();
create trigger audit_dataset_seal after update of sealed_at on public.evaluation_datasets for each row when (old.sealed_at is distinct from new.sealed_at) execute function public.audit_event();
create trigger audit_versions after insert or update of status on public.knowledge_versions for each row execute function public.audit_event();

-- Deleting a business: everything it owns goes (the foreign keys cascade), and a receipt that says what and when remains, with no content in it.
create table public.deletion_receipts (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null,
  business_name text not null,
  deleted_by uuid,
  deleted_at timestamptz not null default now(),
  counts jsonb not null default '{}'::jsonb
);

create or replace function public.delete_business_data(p_business uuid) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  counts jsonb;
  n text;
begin
  if not public.has_role(p_business, array['owner']) and coalesce((select auth.role()), '') <> 'service_role' then
    raise exception 'only an owner can delete a business' using errcode = '42501';
  end if;
  select name into n from public.businesses where id = p_business;
  if n is null then
    raise exception 'no such business' using errcode = '02000';
  end if;
  select jsonb_build_object('sources', (select count(*) from public.knowledge_sources where business_id = p_business), 'faqs', (select count(*) from public.faqs where business_id = p_business),
         'chunks', (select count(*) from public.document_chunks where business_id = p_business), 'embeddings', (select count(*) from public.embeddings where business_id = p_business),
         'conversations', (select count(*) from public.conversations where business_id = p_business), 'messages', (select count(*) from public.messages where business_id = p_business),
         'feedback', (select count(*) from public.feedback where business_id = p_business), 'evaluation_questions', (select count(*) from public.evaluation_questions where business_id = p_business))
    into counts;
  insert into public.deletion_receipts (business_id, business_name, deleted_by, counts) values (p_business, n, (select auth.uid()), counts);
  delete from public.businesses where id = p_business;   -- cascades to every table above, the key, the audit trail and the members
  return counts;
end $$;
revoke all on function public.delete_business_data(uuid) from public, anon;
grant execute on function public.delete_business_data(uuid) to authenticated, service_role;

-- ---- row level security
alter table public.llm_provider_configs enable row level security;
alter table public.llm_provider_secrets enable row level security;   -- no policy on purpose
alter table public.usage_records enable row level security;
alter table public.audit_logs enable row level security;
alter table public.deletion_receipts enable row level security;     -- no policy: the server and the database owner only

create policy llm_config_read on public.llm_provider_configs for select to authenticated using (public.is_member(business_id));
create policy llm_config_write on public.llm_provider_configs for insert to authenticated
  with check (public.has_role(business_id, array['owner', 'admin']) and updated_by = (select auth.uid()));
create policy llm_config_change on public.llm_provider_configs for update to authenticated
  using (public.has_role(business_id, array['owner', 'admin'])) with check (public.has_role(business_id, array['owner', 'admin']));
create policy usage_read on public.usage_records for select to authenticated using (public.has_role(business_id, array['owner', 'admin']));
create policy audit_read on public.audit_logs for select to authenticated using (public.has_role(business_id, array['owner', 'admin']));
