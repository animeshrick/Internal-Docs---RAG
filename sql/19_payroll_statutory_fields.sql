-- 19_payroll_statutory_fields.sql
-- Run once in the Supabase SQL Editor. Safe to run again.
--
-- Adds the fields the redesigned payslip PDF needs (PAN, bank account number, IFSC code, tax
-- regime). Nullable - not yet run in production, so there is no existing data to protect with a
-- placeholder. An employee missing any of these just shows "Not recorded" on their payslip PDF
-- (app/hr/payroll/pdf.py already handles None/missing for all four). Fill in a real employee's
-- actual values directly in the database (or once an Admin-page editor exists for these).

alter table public.employee
    add column if not exists pan text,
    add column if not exists bank_account_number text,
    add column if not exists ifsc_code text,
    add column if not exists tax_regime text;

do $$
begin
    if exists (select 1 from pg_constraint where conname = 'employee_tax_regime_check') then
        alter table public.employee drop constraint employee_tax_regime_check;
    end if;
    -- NULL is allowed (an employee with no regime set yet): Postgres never fails a CHECK over a
    -- NULL value, only over a non-NULL one that isn't 'new' or 'old'.
    alter table public.employee
        add constraint employee_tax_regime_check
        check (tax_regime is null or tax_regime in ('new', 'old'));
end $$;
