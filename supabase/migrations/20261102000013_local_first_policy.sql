-- 13 · LOCAL FIRST. The order in which a business's answers may be made, and what each step may cost.
--
-- The product's order of preference is: the business's OWN computer first; then free hosted inference; then paid hosted inference; with the business's own provider account (BYOK) as an optional
-- preferred or alternative provider. Nothing is sent outside the business unless the business has chosen that step, agreed to that provider's text, and stayed inside its limits.
--
--   * ai_inference_settings   `mode` is still the FIRST step (deterministic, local, byok, or platform). Three new flags name the steps that may follow it, in this fixed order:
--                             byok (the business's own account), free hosted, paid hosted. Every flag defaults to false: a business that never touches them has exactly one step and nothing else is tried.
--                             Paid hosted needs the business's own spending cap. `deterministic` carries no fallback at all.
--   * business_plans          free hosted inference has its own quota (requests and tokens a month), separate from the paid limits; empty means no free hosted inference.
--   * usage_records           each call says which step made it (local, byok, free, paid), so quotas and spend are metered per step.
--   * local_companions        the hardware verdict the companion gave for the chosen model, so that a computer that cannot run it comfortably is not trusted with every question.
-- Written only by the engine service, like the rest of the settings. Nothing here turns inference on.

alter table public.ai_inference_settings
  add column fallback_byok boolean not null default false,
  add column fallback_free boolean not null default false,
  add column fallback_paid boolean not null default false,
  add column paid_cap_usd numeric check (paid_cap_usd is null or (paid_cap_usd > 0 and paid_cap_usd <= 1000000)),
  add constraint ai_settings_paid_needs_cap check (not fallback_paid or paid_cap_usd is not null),
  add constraint ai_settings_deterministic_has_no_fallback check (mode <> 'deterministic' or not (fallback_byok or fallback_free or fallback_paid));

alter table public.business_plans
  add column free_requests_per_month integer check (free_requests_per_month is null or free_requests_per_month >= 0),
  add column free_tokens_per_month bigint check (free_tokens_per_month is null or free_tokens_per_month >= 0);

alter table public.usage_records
  add column tier text check (tier is null or tier in ('local', 'byok', 'free', 'paid'));
create index usage_records_tier_idx on public.usage_records (business_id, kind, tier, created_at desc);

alter table public.local_companions
  add column fit text check (fit is null or fit in ('comfortable', 'possible', 'not_advised', 'unknown'));

-- Turning on a step that sends evidence outside the business, or changing the spending cap, is an audited act with the person who did it (the same trigger as before, one more branch of detail).
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
    d := jsonb_build_object('op', tg_op, 'mode_before', o ->> 'mode', 'mode_after', n ->> 'mode', 'provider', n ->> 'platform_provider', 'model', n ->> 'platform_model', 'fallback_after', n ->> 'fallback',
                            'byok_step_before', o ->> 'fallback_byok', 'byok_step_after', n ->> 'fallback_byok', 'free_step_before', o ->> 'fallback_free', 'free_step_after', n ->> 'fallback_free',
                            'paid_step_before', o ->> 'fallback_paid', 'paid_step_after', n ->> 'fallback_paid', 'paid_cap_usd_after', n ->> 'paid_cap_usd');
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
