-- 08_pm_role.sql
-- Run once in the Supabase SQL Editor. Safe to run again.
--
-- Allows the login role "pm" (project manager). A pm approves leave for employees that have no manager,
-- but has no HR powers (no HR admin page, no other people's data, no payslip upload).

alter table public.employee_auth drop constraint if exists employee_auth_role_check;
alter table public.employee_auth add constraint employee_auth_role_check
    check (role in ('employee', 'hr', 'ceo', 'pm'));

notify pgrst, 'reload schema';
