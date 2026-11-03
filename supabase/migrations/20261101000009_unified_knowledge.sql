-- 09 · One knowledge base of many kinds of source: freshness and a single read model.
--
-- FAQ tables, documents, spreadsheets, JSON and web pages already share one pipeline (they are read into entries, embedded and searched together). What was missing in the database is
--   * WHEN a source stops being current: a source can say it is valid from / until a day, and which earlier source it replaces. The service leaves what is not current out of
--     retrieval and says so when a question is only answered by such material;
--   * ONE place to read "what does this business know, and from where": `knowledge_units`, over FAQ entries and passages alike, with the source, the heading, the text, the version, when it
--     was read, and whether it is current today.
-- Nothing here changes how rows are searched or who may read them: every policy of 03 still applies (the view runs with the caller's rights).

alter table public.knowledge_sources
  add column valid_from date,
  add column valid_until date,
  add column supersedes text check (supersedes is null or char_length(supersedes) <= 1000),   -- the source(s) this one replaces, by name, separated by commas
  add constraint knowledge_sources_validity_order check (valid_from is null or valid_until is null or valid_from <= valid_until);

-- A person may state a source's freshness (an offer that ends, a policy that takes over); the pipeline fills the same columns from what a file says about itself.
grant update (valid_from, valid_until, supersedes) on public.knowledge_sources to authenticated;

-- The name of the source that has replaced this one today, if any. A replacing source only counts once it is itself ready and in force, so a policy announced for next month does not
-- retire this month's.
create or replace function public.source_superseded_by(p_source uuid) returns text
language sql stable security invoker set search_path = '' as $$
  select n.name
  from public.knowledge_sources o
  join public.knowledge_sources n on n.business_id = o.business_id and n.id <> o.id and n.status = 'ready'
       and (n.valid_from is null or n.valid_from <= current_date) and (n.valid_until is null or n.valid_until >= current_date)
  where o.id = p_source
    and exists (select 1 from regexp_split_to_table(lower(coalesce(n.supersedes, '')), '[,;|]') as x
                where btrim(x) <> '' and lower(regexp_replace(btrim(x), '^.*/', '')) = lower(regexp_replace(rtrim(o.name, '/'), '^.*/', '')))
  order by n.updated_at desc
  limit 1
$$;
revoke all on function public.source_superseded_by(uuid) from public, anon;
grant execute on function public.source_superseded_by(uuid) to authenticated, service_role;

-- Every searchable unit of knowledge, whatever it came from. A customer never sees the type of source; the business does, here.
create or replace view public.knowledge_units with (security_invoker = true) as
  select f.business_id, f.id as unit_id, 'faq'::text as unit_kind, f.source_id, s.name as source_name, s.source_type, coalesce(s.url, s.storage_path) as source_location,
         f.question as heading, f.answer as content, f.source_version as version, f.created_at as ingested_at,
         s.valid_from, s.valid_until, s.supersedes, s.status as source_status, public.source_superseded_by(s.id) as superseded_by,
         ((s.valid_from is null or s.valid_from <= current_date) and (s.valid_until is null or s.valid_until >= current_date)
          and public.source_superseded_by(s.id) is null) as is_current   -- by dates and replacement; `source_status` says separately whether the last reading failed
  from public.faqs f left join public.knowledge_sources s on s.id = f.source_id and s.business_id = f.business_id
  where f.active
  union all
  select c.business_id, c.id, 'passage'::text, c.source_id, s.name, s.source_type, coalesce(s.url, s.storage_path),
         c.heading, c.content, c.source_version, c.created_at,
         s.valid_from, s.valid_until, s.supersedes, s.status, public.source_superseded_by(s.id),
         ((s.valid_from is null or s.valid_from <= current_date) and (s.valid_until is null or s.valid_until >= current_date)
          and public.source_superseded_by(s.id) is null)
  from public.document_chunks c join public.knowledge_sources s on s.id = c.source_id and s.business_id = c.business_id
  where c.active;
revoke all on public.knowledge_units from anon;
grant select on public.knowledge_units to authenticated, service_role;

comment on view public.knowledge_units is 'FAQ entries and passages of one business in one shape: source, heading, text, version, when read, and whether the source is current today. Runs with the caller''s rights.';
