-- 18_whatsapp_report_numbers.sql
-- Run once in the Supabase SQL Editor.
-- Up to 10 WhatsApp numbers (managed by the super admin) that receive the twice-daily HR report
-- (attendance, leave, and WFH for the day), sent through AiSensy at 11am and 5pm India time.
-- No RLS policies: only our server (secret key) reads or writes this table, same as every other table here.

create table if not exists public.whatsapp_report_numbers (
    id bigint generated always as identity primary key,
    label text not null,                    -- whose number this is, e.g. "Debayan"
    phone_number text not null unique,      -- E.164 with a leading "+", e.g. "+919876543210"
    created_at timestamptz not null default now()
);
