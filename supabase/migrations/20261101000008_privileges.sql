-- 08 · Privileges: a second lock behind row level security. Signed-out visitors get nothing; signed-in people write only where a policy lets them, and never the columns the server owns.

revoke all on all tables in schema public from anon;
revoke all on all functions in schema public from anon;

-- tables only the server writes
revoke insert, update, delete on public.document_chunks, public.embeddings, public.conversations, public.messages, public.usage_records, public.audit_logs,
  public.knowledge_source_versions, public.business_settings, public.profiles from authenticated;
revoke all on public.llm_provider_secrets, public.deletion_receipts from authenticated;

-- what a person may change in their own profile, in settings, in the model configuration
grant update (display_name) on public.profiles to authenticated;
grant update (retrieval_method, hybrid_alpha, thresholds, privacy) on public.business_settings to authenticated;
revoke update on public.businesses from authenticated;
grant update (name, answer_mode, onboarding_completed_at) on public.businesses to authenticated;   -- not owner_id, not slug, not deleted_at
revoke insert, update on public.llm_provider_configs from authenticated;
grant insert (business_id, provider, model, base_url, enabled, capabilities, updated_by) on public.llm_provider_configs to authenticated;
grant update (provider, model, base_url, enabled, capabilities, updated_by) on public.llm_provider_configs to authenticated;   -- not has_key, not key_status
revoke update on public.knowledge_sources from authenticated;
grant update (name, url, status, error) on public.knowledge_sources to authenticated;   -- version, checksum and counts belong to the pipeline
revoke delete on public.evaluation_results from authenticated;
