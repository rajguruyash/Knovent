-- 03 · Knowledge: sources, their versions, FAQ entries, document chunks, embeddings, and the questions learned from feedback.
-- Child rows point at their parent with (id, business_id), so a row can never belong to a different business than its parent.

create table public.knowledge_sources (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  name text not null check (char_length(name) between 1 and 300),
  source_type text not null check (source_type in ('faq_table', 'document', 'web_page', 'website', 'json', 'text')),
  url text,
  storage_path text check (storage_path is null or storage_path like business_id::text || '/%'),  -- files live under their business's folder; the storage policies read the same prefix
  status text not null default 'pending' check (status in ('pending', 'processing', 'ready', 'failed', 'archived')),
  parse_status text not null default 'pending' check (parse_status in ('pending', 'parsed', 'partial', 'failed', 'needs_browser')),  -- needs_browser: a page that builds itself with JavaScript
  error text,
  version integer not null default 1 check (version >= 1),
  checksum text,
  bytes bigint check (bytes is null or bytes >= 0),
  entry_count integer not null default 0,
  chunk_count integer not null default 0,
  created_by uuid default auth.uid() references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  last_crawled_at timestamptz,
  unique (business_id, name),
  unique (id, business_id)
);
create trigger knowledge_sources_touch before update on public.knowledge_sources for each row execute function public.touch_updated_at();

-- what changed between two readings of a source (the history of a crawled page or a replaced file)
create table public.knowledge_source_versions (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  source_id uuid not null,
  version integer not null,
  checksum text,
  change_summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (source_id, business_id) references public.knowledge_sources (id, business_id) on delete cascade,
  unique (source_id, version)
);

create table public.faqs (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  source_id uuid,
  external_id text not null check (char_length(external_id) between 1 and 200),
  question text not null check (char_length(question) between 1 and 1000),
  answer text not null check (char_length(answer) between 1 and 20000),
  category text,
  alt_questions text[] not null default '{}',
  active boolean not null default true,
  source_version integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (source_id, business_id) references public.knowledge_sources (id, business_id) on delete cascade,
  unique (id, business_id)
);
create unique index faqs_active_external_id on public.faqs (business_id, external_id) where active;
create index faqs_source_idx on public.faqs (source_id);
create trigger faqs_touch before update on public.faqs for each row execute function public.touch_updated_at();

create table public.document_chunks (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  source_id uuid not null,
  ordinal integer not null check (ordinal >= 0),
  heading text,
  content text not null check (char_length(content) between 1 and 20000),
  content_hash text not null,
  location jsonb not null default '{}'::jsonb,         -- page, section, URL fragment: where in the source this is
  injection_flags text[] not null default '{}',        -- instruction-like text found here: kept out of model prompts
  active boolean not null default true,
  source_version integer not null default 1,
  created_at timestamptz not null default now(),
  foreign key (source_id, business_id) references public.knowledge_sources (id, business_id) on delete cascade,
  unique (id, business_id),
  unique (source_id, source_version, ordinal)
);
create unique index chunks_no_duplicate_text on public.document_chunks (business_id, content_hash) where active;  -- the same text is not kept twice
create index chunks_source_idx on public.document_chunks (source_id);

create table public.embeddings (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  faq_id uuid,
  chunk_id uuid,
  view_kind text not null check (view_kind in ('q', 'qa', 'chunk')),   -- one vector per way of asking, per question-with-answer, per chunk
  view_text text not null,
  model text not null,
  embedding extensions.vector(384) not null,
  created_at timestamptz not null default now(),
  foreign key (faq_id, business_id) references public.faqs (id, business_id) on delete cascade,
  foreign key (chunk_id, business_id) references public.document_chunks (id, business_id) on delete cascade,
  check ((faq_id is not null)::int + (chunk_id is not null)::int = 1)
);
create index embeddings_business_idx on public.embeddings (business_id);
create index embeddings_faq_idx on public.embeddings (faq_id) where faq_id is not null;
create index embeddings_chunk_idx on public.embeddings (chunk_id) where chunk_id is not null;
-- Approximate search for tenants too large to scan exactly. Small tenants (the usual case) are served from their own rows by the btree above.
create index embeddings_hnsw on public.embeddings using hnsw (embedding extensions.vector_cosine_ops);

-- questions people approved or corrected: searched as extra ways of asking an entry. Kept apart from the sources and from every evaluation set.
create table public.learned_questions (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  faq_id uuid not null,
  question text not null check (char_length(question) between 1 and 300),
  question_norm text generated always as (public.norm_text(question)) stored,
  feedback_id uuid,
  active boolean not null default true,
  created_by uuid default auth.uid() references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  foreign key (faq_id, business_id) references public.faqs (id, business_id) on delete cascade,
  unique (faq_id, question_norm)
);

-- When a source is read again: everything of its older versions stops being searched in one step, so the new reading replaces the old and nothing outdated stays active.
create or replace function public.supersede_source_content(p_source uuid, p_new_version integer) returns void
language plpgsql set search_path = '' as $$
begin
  update public.document_chunks set active = false where source_id = p_source and source_version < p_new_version and active;
  update public.faqs set active = false where source_id = p_source and source_version < p_new_version and active;
  update public.knowledge_sources set version = p_new_version, status = 'processing', updated_at = now() where id = p_source;
end $$;
revoke all on function public.supersede_source_content(uuid, integer) from public, anon, authenticated;
grant execute on function public.supersede_source_content(uuid, integer) to service_role;

-- Exact nearest-neighbour search over one business's active views. SECURITY INVOKER: a signed-in person only ever reaches the rows their policies allow.
create or replace function public.match_embeddings(p_business uuid, p_query extensions.vector(384), p_k integer default 20)
returns table (embedding_id uuid, faq_id uuid, chunk_id uuid, view_kind text, view_text text, similarity double precision)
language sql stable security invoker set search_path = '' as $$
  select e.id, e.faq_id, e.chunk_id, e.view_kind, e.view_text, 1 - (e.embedding operator(extensions.<=>) p_query)
  from public.embeddings e
  where e.business_id = p_business
    and (e.faq_id is null or exists (select 1 from public.faqs f where f.id = e.faq_id and f.active))
    and (e.chunk_id is null or exists (select 1 from public.document_chunks c where c.id = e.chunk_id and c.active))
  order by e.embedding operator(extensions.<=>) p_query
  limit least(greatest(p_k, 1), 200)
$$;

-- ---- row level security: everyone in the business reads; editors and above write sources, FAQ entries and learned questions; only the server writes what it derives (chunks, vectors)
alter table public.knowledge_sources enable row level security;
alter table public.knowledge_source_versions enable row level security;
alter table public.faqs enable row level security;
alter table public.document_chunks enable row level security;
alter table public.embeddings enable row level security;
alter table public.learned_questions enable row level security;

create policy sources_read on public.knowledge_sources for select to authenticated using (public.is_member(business_id));
create policy sources_write on public.knowledge_sources for all to authenticated
  using (public.has_role(business_id, array['owner', 'admin', 'editor'])) with check (public.has_role(business_id, array['owner', 'admin', 'editor']));
create policy source_versions_read on public.knowledge_source_versions for select to authenticated using (public.is_member(business_id));
create policy faqs_read on public.faqs for select to authenticated using (public.is_member(business_id));
create policy faqs_write on public.faqs for all to authenticated
  using (public.has_role(business_id, array['owner', 'admin', 'editor'])) with check (public.has_role(business_id, array['owner', 'admin', 'editor']));
create policy chunks_read on public.document_chunks for select to authenticated using (public.is_member(business_id));
create policy embeddings_read on public.embeddings for select to authenticated using (public.is_member(business_id));
create policy learned_read on public.learned_questions for select to authenticated using (public.is_member(business_id));
create policy learned_write on public.learned_questions for all to authenticated
  using (public.has_role(business_id, array['owner', 'admin', 'editor'])) with check (public.has_role(business_id, array['owner', 'admin', 'editor']));
