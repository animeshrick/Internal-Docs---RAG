-- 11_superadmin.sql
-- Run once in the Supabase SQL editor.
-- Allows the login role "superadmin" (applies leave and marks attendance for anyone, on any past date)
-- and the attendance source "superadmin".

alter table public.employee_auth drop constraint if exists employee_auth_role_check;
alter table public.employee_auth add constraint employee_auth_role_check
    check (role in ('employee', 'hr', 'ceo', 'pm', 'superadmin'));

alter table public.attendance drop constraint if exists attendance_source_check;
alter table public.attendance add constraint attendance_source_check
    check (source in ('self', 'correction', 'superadmin'));
