-- 09_chat_history.sql
-- Run once in the Supabase SQL Editor. Safe to run again.
--
-- Saves every chat question and answer of an employee, with the exact time.
-- Only the newest MESSAGES_KEPT (10) messages of each employee are kept: the trigger below
-- deletes the rest as soon as a newer one arrives. So the table holds at most
-- 10 rows per employee for ever, whatever the usage, and no scheduled job is needed.
--
-- The limit lives here rather than in the application because the database cannot be
-- skipped by a bug, a crashed container or a failed HTTP call.
--
-- Only the server (secret key) can read this table. Each employee reads only their own messages through the API.
-- If you change the 10 below, change MESSAGES_KEPT_PER_EMPLOYEE in app/chat/store.py to match.

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

-- ------------------------------------------------------------------ keep only the newest 10 per employee

create or replace function public.trim_chat_messages() returns trigger
language plpgsql
as $$
begin
    -- The 11th newest message of this employee, and everything older than it, goes.
    -- With 10 or fewer rows the subquery is NULL, "id <= NULL" matches nothing, and nothing is deleted.
    delete from public.chat_messages
    where employee_id = new.employee_id
      and id <= (
          select id
          from public.chat_messages
          where employee_id = new.employee_id
          order by id desc
          offset 10
          limit 1
      );
    return null;                                        -- an AFTER trigger ignores the return value
end;
$$;

drop trigger if exists chat_messages_trim on public.chat_messages;

create trigger chat_messages_trim
    after insert on public.chat_messages
    for each row
    execute function public.trim_chat_messages();

-- One-time tidy-up, in case the table already holds more than 10 messages for someone.
-- Does nothing on a new or already-trimmed table.
delete from public.chat_messages as older
using (
    select id, row_number() over (partition by employee_id order by id desc) as position
    from public.chat_messages
) as ranked
where older.id = ranked.id
  and ranked.position > 10;

notify pgrst, 'reload schema';
