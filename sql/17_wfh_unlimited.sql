-- 17_wfh_unlimited.sql
-- Run once in the Supabase SQL Editor, AFTER 15_wfh_leave_type.sql. Safe to run again.
--
-- What it changes:
--   Work From Home no longer has a yearly quota (no more "12 left" balance). An employee can
--   mark as many WFH days as needed; every one of them still goes to the super admin only for
--   approval (unchanged from 15_wfh_leave_type.sql).
--
-- The application code (app/hr/leave/service.py, check_new_leave) skips the balance/limit check
-- entirely for limit_type = 'unlimited'. The chat assistant and the website show WFH with no
-- day count, just "approved by the super admin".

-- 1. allow the new limit_type value
do $$
begin
    if exists (select 1 from pg_constraint where conname = 'leave_types_limit_type_check') then
        alter table public.leave_types drop constraint leave_types_limit_type_check;
    end if;
    alter table public.leave_types
        add constraint leave_types_limit_type_check
        check (limit_type in ('per_year', 'per_request', 'unlimited'));
end $$;

-- 2. WFH has no annual cap any more (days_allowed is kept, satisfying the "not null, > 0" check,
-- but is no longer read anywhere once limit_type is 'unlimited').
update public.leave_types set limit_type = 'unlimited' where code = 'wfh';
