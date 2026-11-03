-- 01 · Extensions and small helpers.
-- pgvector lives in the `extensions` schema (as on Supabase). Nothing here stores data.

create schema if not exists extensions;
create extension if not exists vector with schema extensions;

-- A question as it is compared for duplicates: lower case, punctuation and extra space removed.
create or replace function public.norm_text(t text) returns text
language sql immutable parallel safe set search_path = '' as $$
  select btrim(regexp_replace(lower(regexp_replace(coalesce(t, ''), '[^[:alnum:][:space:]]', ' ', 'g')), '\s+', ' ', 'g'))
$$;

create or replace function public.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at = now();
  return new;
end $$;

-- True when the statement runs as the server (the service role, or the database owner inside a SECURITY DEFINER function), not as a client. The roles a client can ever act as are
-- exactly these three; anything else is the server's own. Only for ordinary (SECURITY INVOKER) functions and triggers: inside a SECURITY DEFINER function the current role is always
-- the function's owner, so those functions check the JWT's role claim (auth.role()) instead.
create or replace function public.is_server() returns boolean
language sql stable set search_path = '' as $$
  select current_user not in ('anon', 'authenticated', 'authenticator')
$$;
