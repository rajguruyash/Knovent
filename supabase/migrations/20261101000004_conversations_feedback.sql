-- 04 · Conversations, messages and feedback. Customers are not platform users: the server writes their messages. Members of the business read them.

create table public.conversations (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  channel text not null default 'playground' check (channel in ('playground', 'api', 'widget', 'whatsapp', 'other')),
  customer_ref text,                       -- an opaque reference (a hash), never a name or a phone number
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  unique (id, business_id)
);
create index conversations_business_idx on public.conversations (business_id, created_at desc);

create table public.messages (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  conversation_id uuid not null,
  role text not null check (role in ('customer', 'assistant')),
  content text not null check (char_length(content) <= 20000),
  outcome text check (outcome in ('faq', 'grounded', 'partial', 'insufficient', 'conflict', 'clarification')),
  evidence jsonb,                           -- supported and unsupported parts, sources used, reasons
  retrieval jsonb,                          -- candidates with their scores and the method
  llm jsonb,                                -- whether a model was used, which, what the wording check found, any fallback
  latency_ms integer,
  created_at timestamptz not null default now(),
  foreign key (conversation_id, business_id) references public.conversations (id, business_id) on delete cascade,
  unique (id, business_id)
);
create index messages_conversation_idx on public.messages (conversation_id, created_at);

create table public.feedback (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses (id) on delete cascade,
  message_id uuid,
  verdict text not null check (verdict in ('approve', 'reject', 'correct', 'missing_knowledge', 'wrong_source')),
  faq_id uuid,
  corrected_answer text check (corrected_answer is null or char_length(corrected_answer) <= 20000),
  comment text check (comment is null or char_length(comment) <= 2000),
  status text not null default 'new' check (status in ('new', 'accepted', 'dismissed')),
  created_by uuid default auth.uid() references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  foreign key (message_id, business_id) references public.messages (id, business_id) on delete cascade,
  foreign key (faq_id, business_id) references public.faqs (id, business_id) on delete set null (faq_id)
);
alter table public.learned_questions add foreign key (feedback_id) references public.feedback (id) on delete set null;

alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.feedback enable row level security;
create policy conversations_read on public.conversations for select to authenticated using (public.is_member(business_id));
create policy messages_read on public.messages for select to authenticated using (public.is_member(business_id));
create policy feedback_read on public.feedback for select to authenticated using (public.is_member(business_id));
create policy feedback_add on public.feedback for insert to authenticated
  with check (public.has_role(business_id, array['owner', 'admin', 'editor']) and created_by = (select auth.uid()));
create policy feedback_review on public.feedback for update to authenticated
  using (public.has_role(business_id, array['owner', 'admin', 'editor'])) with check (public.has_role(business_id, array['owner', 'admin', 'editor']));
create policy feedback_delete on public.feedback for delete to authenticated using (public.has_role(business_id, array['owner', 'admin']));
