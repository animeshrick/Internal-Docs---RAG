-- 09_chat_history.sql
-- Run once in the Supabase SQL Editor. Safe to run again.
--
-- Saves every chat question and answer of an employee, with the exact time. Kept for 30 days:
-- the backend deletes older rows (and never shows them), so no scheduled job is needed.
-- Only the server (secret key) can read this table. Each employee reads only their own messages through the API.

create table if not exists public.chat_messages (
    id bigint generated always as identity primary key,
    employee_id bigint not null references public.employee (id) on delete cascade,
    sender text not null check (sender in ('user', 'assistant')),
    message text not null,
    sources jsonb not null default '[]'::jsonb,         -- the policy documents an answer used
    created_at timestamptz not null default now()
);

create index if not exists chat_messages_employee_idx on public.chat_messages (employee_id, id desc);
create index if not exists chat_messages_created_idx on public.chat_messages (created_at);

alter table public.chat_messages enable row level security;

notify pgrst, 'reload schema';
