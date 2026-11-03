-- 02 · Businesses, members, roles and settings. Every later table carries business_id and is guarded by the helpers defined here.

create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text,
  created_at timestamptz not null default now()
);

create table public.businesses (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 120),
  slug text not null unique check (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  owner_id uuid not null references auth.users (id),
  answer_mode text not null default 'local_only' check (answer_mode in ('local_only', 'llm')),
  onboarding_completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);

create table public.business_members (
  business_id uuid not null references public.businesses (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  role text not null check (role in ('owner', 'admin', 'editor', 'viewer')),
  created_at timestamptz not null default now(),
  primary key (business_id, user_id)
);
create index business_members_user_idx on public.business_members (user_id);

create table public.business_settings (
  business_id uuid primary key references public.businesses (id) on delete cascade,
  retrieval_method text not null default 'hybrid' check (retrieval_method in ('hybrid', 'semantic')),
  hybrid_alpha numeric not null default 0.3 check (hybrid_alpha between 0 and 1),
  thresholds jsonb not null default '{}'::jsonb,
  privacy jsonb not null default '{"log_questions": true, "retention_days": 365}'::jsonb,
  updated_at timestamptz not null default now()
);

-- ---- who is who. SECURITY DEFINER so that the check does not itself depend on the policies it serves; the search path is empty so that nothing can be shadowed.
create or replace function public.is_member(b uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.business_members m join public.businesses x on x.id = m.business_id
    where m.business_id = b and m.user_id = (select auth.uid()) and x.deleted_at is null)
$$;

create or replace function public.has_role(b uuid, roles text[]) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.business_members m join public.businesses x on x.id = m.business_id
    where m.business_id = b and m.user_id = (select auth.uid()) and m.role = any (roles) and x.deleted_at is null)
$$;

revoke all on function public.is_member(uuid), public.has_role(uuid, text[]) from public, anon;
grant execute on function public.is_member(uuid), public.has_role(uuid, text[]) to authenticated, service_role;

-- ---- a profile for every new user
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, display_name) values (new.id, coalesce(new.raw_user_meta_data ->> 'name', split_part(new.email, '@', 1)))
  on conflict do nothing;
  return new;
end $$;
create trigger on_auth_user_created after insert on auth.users for each row execute function public.handle_new_user();

-- ---- the one way to create a business: the caller becomes its owner. (No direct INSERT policy: a client cannot choose someone else as owner.)
create or replace function public.create_business(p_name text, p_slug text) returns public.businesses
language plpgsql security definer set search_path = '' as $$
declare
  uid uuid := (select auth.uid());
  b public.businesses;
begin
  if uid is null then
    raise exception 'sign in first' using errcode = '42501';
  end if;
  insert into public.businesses (name, slug, owner_id) values (p_name, p_slug, uid) returning * into b;
  insert into public.business_members (business_id, user_id, role) values (b.id, uid, 'owner');
  insert into public.business_settings (business_id) values (b.id);
  return b;
end $$;
revoke all on function public.create_business(text, text) from public, anon;
grant execute on function public.create_business(text, text) to authenticated;

-- ---- membership rules: only an owner hands out or takes away ownership, and a business never loses its last owner
create or replace function public.guard_members() returns trigger
language plpgsql set search_path = '' as $$
begin
  if pg_trigger_depth() > 1 or public.is_server() then   -- deleting the whole business (a cascade), or the server acting
    return coalesce(new, old);
  end if;
  if tg_op in ('INSERT', 'UPDATE') and new.role = 'owner' and not public.has_role(new.business_id, array['owner']) then
    raise exception 'only an owner can make someone an owner' using errcode = '42501';
  end if;
  if tg_op in ('UPDATE', 'DELETE') and old.role = 'owner' then
    if not public.has_role(old.business_id, array['owner']) then
      raise exception 'only an owner can change an owner' using errcode = '42501';
    end if;
    if (tg_op = 'DELETE' or new.role <> 'owner')
       and not exists (select 1 from public.business_members m where m.business_id = old.business_id and m.role = 'owner' and m.user_id <> old.user_id) then
      raise exception 'a business needs at least one owner' using errcode = '23514';
    end if;
  end if;
  return coalesce(new, old);
end $$;
create trigger guard_members before insert or update or delete on public.business_members for each row execute function public.guard_members();

create trigger businesses_touch before update on public.businesses for each row execute function public.touch_updated_at();
create trigger business_settings_touch before update on public.business_settings for each row execute function public.touch_updated_at();

-- ---- row level security
alter table public.profiles enable row level security;
alter table public.businesses enable row level security;
alter table public.business_members enable row level security;
alter table public.business_settings enable row level security;

create policy profiles_read on public.profiles for select to authenticated
  using (id = (select auth.uid()) or exists (
    select 1 from public.business_members a join public.business_members b on a.business_id = b.business_id
    where a.user_id = (select auth.uid()) and b.user_id = profiles.id));
create policy profiles_update_own on public.profiles for update to authenticated using (id = (select auth.uid())) with check (id = (select auth.uid()));

create policy businesses_read on public.businesses for select to authenticated using (public.is_member(id) and deleted_at is null);
create policy businesses_update on public.businesses for update to authenticated
  using (public.has_role(id, array['owner', 'admin'])) with check (public.has_role(id, array['owner', 'admin']));

create policy members_read on public.business_members for select to authenticated using (public.is_member(business_id));
create policy members_add on public.business_members for insert to authenticated with check (public.has_role(business_id, array['owner', 'admin']));
create policy members_change on public.business_members for update to authenticated
  using (public.has_role(business_id, array['owner', 'admin'])) with check (public.has_role(business_id, array['owner', 'admin']));
create policy members_remove on public.business_members for delete to authenticated
  using (public.has_role(business_id, array['owner', 'admin']) or user_id = (select auth.uid()));

create policy settings_read on public.business_settings for select to authenticated using (public.is_member(business_id));
create policy settings_update on public.business_settings for update to authenticated
  using (public.has_role(business_id, array['owner', 'admin'])) with check (public.has_role(business_id, array['owner', 'admin']));
