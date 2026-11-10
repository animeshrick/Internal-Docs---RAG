-- 07_confidential_flag.sql
-- Run once in the Supabase SQL Editor. Safe to run again.
--
-- Marks employees whose data the HR assistant must never show to anyone else (not even to HR).
-- The CEO is protected automatically (role = 'ceo'). Set the flag to true for the CTO, and for anyone else who needs it:
--   update public.employee set is_confidential = true where emp_id = 'EMP-xxxx';
-- Their own chat still works: they can see their own data.

alter table public.employee add column if not exists is_confidential boolean not null default false;

notify pgrst, 'reload schema';
