-- 07 · File storage: one private bucket, one folder per business (<business id>/<source id>/<file>). The policies read the first path segment, so a file can only be reached by the business it belongs to.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('knowledge-files', 'knowledge-files', false, 26214400,
        array['text/csv', 'text/tab-separated-values', 'text/plain', 'text/markdown', 'text/html', 'application/json', 'application/pdf',
              'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel'])
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;

-- The business a storage path belongs to; null when the first segment is not a business id (such a path is refused by every policy).
create or replace function public.path_business(object_name text) returns uuid
language plpgsql immutable set search_path = '' as $$
begin
  return ((string_to_array(object_name, '/'))[1])::uuid;
exception when others then
  return null;
end $$;

create policy knowledge_files_read on storage.objects for select to authenticated
  using (bucket_id = 'knowledge-files' and public.is_member(public.path_business(name)));
create policy knowledge_files_add on storage.objects for insert to authenticated
  with check (bucket_id = 'knowledge-files' and public.has_role(public.path_business(name), array['owner', 'admin', 'editor']));
create policy knowledge_files_change on storage.objects for update to authenticated
  using (bucket_id = 'knowledge-files' and public.has_role(public.path_business(name), array['owner', 'admin', 'editor']))
  with check (bucket_id = 'knowledge-files' and public.has_role(public.path_business(name), array['owner', 'admin', 'editor']));
create policy knowledge_files_remove on storage.objects for delete to authenticated
  using (bucket_id = 'knowledge-files' and public.has_role(public.path_business(name), array['owner', 'admin']));
