-- 21_government_id.sql
-- Run once in the Supabase SQL editor. Safe to run again.
--
-- Every employee must give one government ID (Aadhaar OR PAN) before they can use the app.
-- The gate is enforced by the backend: /login and /me report must_submit_government_id, and
-- every other endpoint refuses until a row exists here for that employee.
--
-- What is stored:
--   id_type          "aadhaar" or "pan" - the employee picks one on the Govt ID page.
--   id_number_encrypted  the full number, encrypted by the backend (Fernet) before it ever
--                    reaches the database, so the number cannot be read from the Supabase
--                    dashboard or from a database backup. The key lives in GOVERNMENT_ID_ENCRYPTION_KEY
--                    in the backend .env and is never stored here.
--   id_number_last4  the last 4 characters in plain text, so the employee and the super admin
--                    can recognise the card ("Aadhaar ending 1234") without decrypting anything.
--   front_image_path / back_image_path
--                    paths inside the private Storage bucket "government-ids". Aadhaar needs both
--                    sides; PAN needs the front only, so back_image_path stays null for PAN.
--
-- The images live in Supabase Storage, in a NEW PRIVATE bucket called "government-ids" - create
-- that bucket by hand in the Supabase dashboard (Storage -> New bucket -> Private), the same way
-- "leave-documents", "shared-documents" and "direct-message-attachments" were created. This
-- migration only adds the database table.

-- ---------------------------------------------------------------- the table
-- One row per employee (employee_id is the primary key), so an employee can never have two IDs
-- on file. Submitting is a one-time action: the row is only removed when the super admin clears
-- it, and the employee is then asked again at their next login.
--
-- IMPORTANT: employee_id must stay the ONLY foreign key to public.employee in this table. The
-- backend reads the employee row and this row in one request (PostgREST "embedding"), and that
-- only works while there is a single relationship between the two tables. A second column
-- pointing at employee (for example "who cleared this ID") makes the embed ambiguous, and
-- PostgREST then answers 300 Multiple Choices instead of the row. Who cleared an ID is recorded
-- in superadmin_audit_log, which is where that belongs.
create table if not exists public.employee_government_id (
    employee_id bigint primary key references public.employee (id) on delete cascade,
    id_type text not null check (id_type in ('aadhaar', 'pan')),
    id_number_encrypted text not null,
    id_number_last4 text not null,
    front_image_path text not null,
    back_image_path text null,
    front_content_type text not null,
    back_content_type text null,
    submitted_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    update_at timestamptz not null default now(),
    -- Aadhaar is a two-sided card, PAN is used front-only. Keep that rule in the database too.
    constraint government_id_back_image_rule check (
        (id_type = 'aadhaar' and back_image_path is not null)
        or (id_type = 'pan' and back_image_path is null)
    )
);

-- Keeps update_at correct (the function was created in 03_employee_and_auth.sql).
drop trigger if exists employee_government_id_set_update_at on public.employee_government_id;
create trigger employee_government_id_set_update_at
    before update on public.employee_government_id
    for each row execute function public.set_update_at();

-- Repairs a table created by an earlier version of this file, which had a second foreign key to
-- employee (cleared_by_id) and so broke the embed described above. Nothing reads that column.
alter table public.employee_government_id drop column if exists cleared_by_id;

-- Only our server (secret key) may read this table. The public anon key reads nothing.
alter table public.employee_government_id enable row level security;

-- ---------------------------------------------------------------- the audit log accepts two more actions
-- 13_superadmin_audit.sql limited "action" to leave and attendance changes. Looking at or clearing
-- somebody's government ID is also a super admin action, so both are written to the same log.
alter table public.superadmin_audit_log drop constraint if exists superadmin_audit_log_action_check;
alter table public.superadmin_audit_log add constraint superadmin_audit_log_action_check
    check (action in ('leave_applied', 'attendance_set', 'government_id_viewed', 'government_id_cleared'));

notify pgrst, 'reload schema';
