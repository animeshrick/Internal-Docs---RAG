-- 19_payroll_statutory_fields.sql
-- Run once in the Supabase SQL Editor. Safe to run again.
--
-- Adds the fields the redesigned payslip PDF needs (PAN, bank account number, IFSC code, tax
-- regime). All four are required going forward - there is no admin page to edit them yet, so
-- existing AND new rows get an obvious placeholder value by default. Replace a real employee's
-- placeholder values directly in the database until an Admin-page editor for these exists.

alter table public.employee
    add column if not exists pan text not null default 'PANNOTSET0',
    add column if not exists bank_account_number text not null default '0000000000',
    add column if not exists ifsc_code text not null default 'NOTSET0000',
    add column if not exists tax_regime text not null default 'new';

do $$
begin
    if exists (select 1 from pg_constraint where conname = 'employee_tax_regime_check') then
        alter table public.employee drop constraint employee_tax_regime_check;
    end if;
    alter table public.employee
        add constraint employee_tax_regime_check
        check (tax_regime in ('new', 'old'));
end $$;
