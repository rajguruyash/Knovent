-- 05 · Evaluation data and the controlled release process.
--
-- Three roles of question set, never mixed:
--   development  debugging, threshold choice, retrieval tuning
--   regression   the gate a new version must pass before it goes live (it influences releases, so it is NOT a test set)
--   locked       the final benchmark: sealed, never edited, results used for reporting only
-- The rules below are enforced here, not left to good behaviour: an evaluation question cannot be indexed as knowledge, cannot appear twice, cannot share a group with a question
-- in another set, and a locked set's results cannot be used for anything but the final report.

create table public.evaluation_datasets (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  name text not null check (char_length(name) between 1 and 200),
  role text not null check (role in ('development', 'regression', 'locked')),
  sealed_at timestamptz,                    -- set once, for a locked set: from then on its questions cannot change
  content_checksum text,
  created_by uuid default auth.uid() references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  unique (business_id, name),
  unique (id, business_id)
);

create table public.evaluation_questions (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  dataset_id uuid not null,
  question text not null check (char_length(question) between 1 and 1000),
  question_norm text generated always as (public.norm_text(question)) stored,
  group_key text not null check (char_length(group_key) between 1 and 200),   -- questions that ask for the same underlying fact share a group, and a group lives in ONE set
  expected_faq_id uuid,
  expected_outcome text not null check (expected_outcome in ('faq', 'grounded', 'partial', 'insufficient', 'conflict', 'clarification')),
  must_contain text[] not null default '{}',
  cite text[] not null default '{}',
  created_at timestamptz not null default now(),
  foreign key (dataset_id, business_id) references public.evaluation_datasets (id, business_id) on delete cascade,
  foreign key (expected_faq_id, business_id) references public.faqs (id, business_id) on delete set null (expected_faq_id),
  unique (dataset_id, question_norm)         -- a duplicate question in one set (and with it, two different expected answers to it) is refused
);
create index evaluation_questions_group_idx on public.evaluation_questions (business_id, group_key);
create index evaluation_questions_norm_idx on public.evaluation_questions (business_id, question_norm);

create table public.knowledge_versions (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  version integer not null check (version >= 1),
  status text not null default 'candidate' check (status in ('candidate', 'active', 'retired', 'rejected')),
  notes text,
  regression_result_id uuid,
  promoted_at timestamptz,
  created_at timestamptz not null default now(),
  unique (business_id, version),
  unique (id, business_id)
);
create unique index one_active_version on public.knowledge_versions (business_id) where status = 'active';

create table public.evaluation_results (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  dataset_id uuid not null,
  knowledge_version_id uuid,
  purpose text not null check (purpose in ('development', 'promotion_gate', 'final_report')),
  passed boolean,                           -- for a promotion gate: did the new version hold up
  metrics jsonb not null default '{}'::jsonb,
  run_by uuid default auth.uid() references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  foreign key (dataset_id, business_id) references public.evaluation_datasets (id, business_id) on delete cascade,
  foreign key (knowledge_version_id, business_id) references public.knowledge_versions (id, business_id) on delete set null (knowledge_version_id),
  unique (id, business_id)
);
alter table public.knowledge_versions add foreign key (regression_result_id, business_id) references public.evaluation_results (id, business_id);

-- ---- integrity rules
create or replace function public.guard_evaluation_question() returns trigger
language plpgsql set search_path = '' as $$
declare
  me public.evaluation_datasets;
  nq text := public.norm_text(new.question);   -- the generated column is not computed yet in a BEFORE trigger
begin
  select * into me from public.evaluation_datasets where id = new.dataset_id;
  if tg_op = 'UPDATE' and me.sealed_at is not null then
    raise exception 'a sealed (locked) set cannot be changed' using errcode = '23514';
  end if;
  if me.sealed_at is not null then
    raise exception 'a sealed (locked) set cannot take new questions' using errcode = '23514';
  end if;
  -- a question that is already knowledge is not a test of it
  if exists (select 1 from public.faqs f where f.business_id = new.business_id and f.active
             and (public.norm_text(f.question) = nq or nq = any (select public.norm_text(a) from unnest(f.alt_questions) a))) then
    raise exception 'this question is already in the knowledge base as an FAQ question, so it cannot be used to test it' using errcode = '23514';
  end if;
  if exists (select 1 from public.learned_questions l where l.business_id = new.business_id and l.question_norm = nq) then
    raise exception 'this question was learned from feedback, so it cannot be used to test the knowledge' using errcode = '23514';
  end if;
  -- the same question in another set is a leak between sets
  if exists (select 1 from public.evaluation_questions q where q.business_id = new.business_id and q.question_norm = nq and q.dataset_id <> new.dataset_id and q.id is distinct from new.id) then
    raise exception 'this question is already in another evaluation set' using errcode = '23514';
  end if;
  -- questions about the same underlying fact stay together
  if exists (select 1 from public.evaluation_questions q where q.business_id = new.business_id and q.group_key = new.group_key and q.dataset_id <> new.dataset_id and q.id is distinct from new.id) then
    raise exception 'questions of group % are already in another evaluation set; paraphrases of one fact must not be split across sets', new.group_key using errcode = '23514';
  end if;
  return new;
end $$;
create trigger guard_evaluation_question before insert or update on public.evaluation_questions for each row execute function public.guard_evaluation_question();

create or replace function public.freeze_sealed_questions() returns trigger
language plpgsql set search_path = '' as $$
begin
  if pg_trigger_depth() > 1 then
    return old;   -- the business is being deleted
  end if;
  if exists (select 1 from public.evaluation_datasets d where d.id = old.dataset_id and d.sealed_at is not null) then
    raise exception 'a sealed (locked) set cannot be changed' using errcode = '23514';
  end if;
  return old;
end $$;
create trigger freeze_sealed_questions before delete on public.evaluation_questions for each row execute function public.freeze_sealed_questions();

create or replace function public.guard_evaluation_dataset() returns trigger
language plpgsql set search_path = '' as $$
begin
  if tg_op = 'UPDATE' then
    if old.sealed_at is not null and (new.sealed_at is distinct from old.sealed_at or new.role <> old.role or new.name <> old.name or new.content_checksum is distinct from old.content_checksum) then
      raise exception 'a sealed set cannot be changed' using errcode = '23514';
    end if;
    if new.sealed_at is not null and old.sealed_at is null and new.role <> 'locked' then
      raise exception 'only a locked set is sealed' using errcode = '23514';
    end if;
    if new.role <> old.role and exists (select 1 from public.evaluation_questions where dataset_id = old.id) then
      raise exception 'a set with questions cannot change its role' using errcode = '23514';
    end if;
  end if;
  return new;
end $$;
create trigger guard_evaluation_dataset before update on public.evaluation_datasets for each row execute function public.guard_evaluation_dataset();

-- What a result may be used for follows from the set it was measured on.
create or replace function public.guard_evaluation_result() returns trigger
language plpgsql set search_path = '' as $$
declare
  r text;
  sealed timestamptz;
begin
  if tg_op in ('UPDATE', 'DELETE') then
    if pg_trigger_depth() > 1 then
      return coalesce(old, new);
    end if;
    select role into r from public.evaluation_datasets where id = old.dataset_id;
    if r = 'locked' then
      raise exception 'results on the locked set are a record and cannot be changed or deleted' using errcode = '23514';
    end if;
    return coalesce(new, old);
  end if;
  select role, sealed_at into r, sealed from public.evaluation_datasets where id = new.dataset_id;
  if r = 'locked' and (new.purpose <> 'final_report' or sealed is null) then
    raise exception 'the locked set may only be used for the final report, once it is sealed: never to tune, choose a model or decide a release' using errcode = '23514';
  end if;
  if new.purpose = 'final_report' and r <> 'locked' then
    raise exception 'a final report is measured on the locked set' using errcode = '23514';
  end if;
  if new.purpose = 'promotion_gate' and r <> 'regression' then
    raise exception 'a release gate is measured on the regression set' using errcode = '23514';
  end if;
  if new.purpose = 'development' and r <> 'development' then
    raise exception 'development runs use the development set' using errcode = '23514';
  end if;
  return new;
end $$;
create trigger guard_evaluation_result before insert or update or delete on public.evaluation_results for each row execute function public.guard_evaluation_result();

-- Evaluation questions never become knowledge, and learned feedback never becomes a test.
create or replace function public.guard_knowledge_text() returns trigger
language plpgsql set search_path = '' as $$
declare
  hit text;
begin
  if tg_table_name = 'faqs' then
    select q.question into hit from public.evaluation_questions q
      where q.business_id = new.business_id and (q.question_norm = public.norm_text(new.question) or q.question_norm = any (select public.norm_text(a) from unnest(new.alt_questions) a)) limit 1;
  else
    select q.question into hit from public.evaluation_questions q where q.business_id = new.business_id and q.question_norm = public.norm_text(new.question) limit 1;
  end if;
  if hit is not null then
    raise exception 'the evaluation question "%" cannot be added to the knowledge base', left(hit, 80) using errcode = '23514';
  end if;
  return new;
end $$;
create trigger guard_faq_text before insert or update of question, alt_questions on public.faqs for each row execute function public.guard_knowledge_text();
create trigger guard_learned_text before insert or update of question on public.learned_questions for each row execute function public.guard_knowledge_text();

-- A version goes live only with a passed release gate behind it. Going back to an earlier version needs no new gate: it was live before.
create or replace function public.guard_knowledge_version() returns trigger
language plpgsql set search_path = '' as $$
declare
  gate public.evaluation_results;
begin
  if new.status = 'active' and (tg_op = 'INSERT' or old.status <> 'active') and new.promoted_at is null then
    select * into gate from public.evaluation_results where id = new.regression_result_id and business_id = new.business_id;
    if gate.id is null or gate.purpose <> 'promotion_gate' or gate.passed is not true then
      raise exception 'a version needs a passed regression gate before it goes live' using errcode = '23514';
    end if;
    new.promoted_at = now();
  end if;
  return new;
end $$;
create trigger guard_knowledge_version before insert or update on public.knowledge_versions for each row execute function public.guard_knowledge_version();

-- Roll back: the version that was live before (it has a promoted_at) goes live again and the current one is retired. One step, all or nothing.
create or replace function public.rollback_to_version(p_business uuid, p_version integer) returns void
language plpgsql security definer set search_path = '' as $$
declare
  target public.knowledge_versions;
begin
  if not public.has_role(p_business, array['owner', 'admin']) and coalesce((select auth.role()), '') <> 'service_role' then   -- (is_server() cannot be used here: inside a SECURITY DEFINER function the session is always the function's owner)
    raise exception 'only an owner or admin can roll back' using errcode = '42501';
  end if;
  select * into target from public.knowledge_versions where business_id = p_business and version = p_version;
  if target.id is null or target.promoted_at is null then
    raise exception 'version % was never live, so it cannot be rolled back to', p_version using errcode = '23514';
  end if;
  update public.knowledge_versions set status = 'retired' where business_id = p_business and status = 'active';
  update public.knowledge_versions set status = 'active' where id = target.id;
end $$;
revoke all on function public.rollback_to_version(uuid, integer) from public, anon;
grant execute on function public.rollback_to_version(uuid, integer) to authenticated, service_role;

-- ---- row level security. The locked set is visible to owners and admins only: looking at it question by question is how a benchmark stops being independent.
alter table public.evaluation_datasets enable row level security;
alter table public.evaluation_questions enable row level security;
alter table public.evaluation_results enable row level security;
alter table public.knowledge_versions enable row level security;

create or replace function public.dataset_visible(d uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.evaluation_datasets x where x.id = d
                 and ((x.role <> 'locked' and public.has_role(x.business_id, array['owner', 'admin', 'editor'])) or public.has_role(x.business_id, array['owner', 'admin'])))
$$;
revoke all on function public.dataset_visible(uuid) from public, anon;
grant execute on function public.dataset_visible(uuid) to authenticated, service_role;

create policy datasets_read on public.evaluation_datasets for select to authenticated using (public.dataset_visible(id));
create policy datasets_write on public.evaluation_datasets for all to authenticated
  using (public.has_role(business_id, array['owner', 'admin'])) with check (public.has_role(business_id, array['owner', 'admin']));
create policy eval_questions_read on public.evaluation_questions for select to authenticated using (public.dataset_visible(dataset_id));
create policy eval_questions_write on public.evaluation_questions for all to authenticated
  using (public.dataset_visible(dataset_id)) with check (public.dataset_visible(dataset_id));
create policy eval_results_read on public.evaluation_results for select to authenticated using (public.is_member(business_id));
create policy eval_results_add on public.evaluation_results for insert to authenticated
  with check (public.has_role(business_id, array['owner', 'admin']) and run_by = (select auth.uid()));
create policy versions_read on public.knowledge_versions for select to authenticated using (public.is_member(business_id));
create policy versions_write on public.knowledge_versions for all to authenticated
  using (public.has_role(business_id, array['owner', 'admin'])) with check (public.has_role(business_id, array['owner', 'admin']));
