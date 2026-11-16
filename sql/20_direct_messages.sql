-- 20_direct_messages.sql
-- Run once in the Supabase SQL editor. Safe to run again.
--
-- 1-to-1 messaging between any two employees (including admin/superadmin). Open: anyone may
-- message anyone, nothing to approve. A message (and the whole thread) is only ever visible for
-- a rolling 24 hours - enforced at query time by the backend (app/hr/messages), never by a
-- delete job, so nothing here deletes old rows. Messages are never edited or deleted by a user.
--
-- The file itself (when a message carries an attachment) lives in Supabase Storage, in a new
-- private bucket called "direct-message-attachments" - create that bucket by hand in the
-- Supabase dashboard (Storage -> New bucket -> Private), the same way "leave-documents" and
-- "shared-documents" were created. This migration only adds the database tables.

-- ---------------------------------------------------------------- 1. the messages themselves
create table if not exists public.direct_messages (
    id bigint generated always as identity primary key,
    sender_id bigint not null references public.employee (id) on delete cascade,
    recipient_id bigint not null references public.employee (id) on delete cascade,
    body text null,
    attachment_path text null,
    attachment_filename text null,
    attachment_content_type text null,
    created_at timestamptz not null default now(),
    constraint direct_messages_not_to_self check (sender_id <> recipient_id),
    constraint direct_messages_has_content check (body is not null or attachment_path is not null)
);
-- "my conversations" and "my thread with X" both read this way: newest first, one side of the pair.
create index if not exists direct_messages_sender_idx on public.direct_messages (sender_id, created_at desc);
create index if not exists direct_messages_recipient_idx on public.direct_messages (recipient_id, created_at desc);

-- ---------------------------------------------------------------- 2. when did I last read each conversation
-- One row per (employee, the other person in the conversation). Updated whenever that employee
-- opens the thread. Used only to compute an unread count - never to hide or delete anything.
create table if not exists public.direct_message_reads (
    employee_id bigint not null references public.employee (id) on delete cascade,
    other_employee_id bigint not null references public.employee (id) on delete cascade,
    last_read_at timestamptz not null default now(),
    primary key (employee_id, other_employee_id)
);

alter table public.direct_messages enable row level security;
alter table public.direct_message_reads enable row level security;

notify pgrst, 'reload schema';
