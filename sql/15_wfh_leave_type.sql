-- 15_wfh_leave_type.sql
-- Run once in the Supabase SQL Editor, AFTER 04_leave_system.sql. Safe to run again.
--
-- What it adds:
--   A "Work From Home" leave type that only the super admin may approve (not HR, not the
--   reporting manager, not the PM). Employees can still apply for it from the chat or the
--   website, with no supporting document needed.
--
-- The application code (app/hr/leave/service.py) checks leave_types.approver == 'superadmin_only'
-- before HR's normal "HR can decide everything" shortcut, so plain HR staff cannot approve WFH.

-- 1. allow the new approver value
do $$
begin
    if exists (select 1 from pg_constraint where conname = 'leave_types_approver_check') then
        alter table public.leave_types drop constraint leave_types_approver_check;
    end if;
    alter table public.leave_types
        add constraint leave_types_approver_check
        check (approver in ('manager_or_hr', 'hr_only', 'superadmin_only'));
end $$;

-- 2. the WFH leave type itself
-- 12 days per year, working days only, at most 5 consecutive days per request, no document required.
-- Adjust days_allowed / max_days_per_request here if the real policy differs.
insert into public.leave_types
    (code, name, days_allowed, limit_type, count_mode, applies_to_gender, approver, can_apply_by_chat, max_days_per_request)
values
    ('wfh', 'Work From Home', 12, 'per_year', 'working_days', null, 'superadmin_only', true, 5)
on conflict (code) do update set
    approver = excluded.approver;
