-- 13_superadmin_audit.sql
-- Run once in the Supabase SQL editor. Safe to run again.
--
-- An audit trail of every leave or attendance change the super admin makes for someone
-- else. Written at the same moment as the change, so it survives even if the employee's
-- name or department changes later.

create table if not exists public.superadmin_audit_log (
    id bigint generated always as identity primary key,
    actor_id bigint not null references public.employee (id),
    actor_emp_id text not null,
    target_id bigint not null references public.employee (id),
    target_emp_id text not null,
    action text not null check (action in ('leave_applied', 'attendance_set')),
    details jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists superadmin_audit_log_created_idx on public.superadmin_audit_log (created_at desc);

alter table public.superadmin_audit_log enable row level security;

notify pgrst, 'reload schema';
