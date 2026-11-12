-- 10_attendance.sql
-- Run once in the Supabase SQL Editor. Safe to run again.
--
-- Attendance: check in / check out with the exact time, correction requests, the office hours, and the office network.
-- Every setting lives in a table (attendance_settings, office_networks), so HR changes it without any code change.
-- All times are stored in UTC (timestamptz). The website shows them in Indian time.
-- Only our server (secret key) can read these tables.

-- ---------------------------------------------------------------- 1. settings (always exactly one row, id = 1)
create table if not exists public.attendance_settings (
    id smallint primary key default 1 check (id = 1),
    office_start time not null default '10:00',          -- the office day starts
    office_end time not null default '19:00',            -- the office day ends
    late_after time not null default '10:45',            -- a check-in after this time is marked late (the 45 minute buffer)
    half_day_hours numeric(4, 2) not null default 4.5,   -- worked hours needed for a half day (less than this = absent)
    early_leave_after time not null default '15:00',     -- a check-out at or before this time is a half day; after it but before office_end = early leave
    network_check_enabled boolean not null default false,-- true = check in and out only from office_networks (false while testing)
    correction_window_days integer not null default 7 check (correction_window_days >= 0),
    updated_at timestamptz not null default now()
);
insert into public.attendance_settings (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------- 2. office networks (used when network_check_enabled = true)
create table if not exists public.office_networks (
    id bigint generated always as identity primary key,
    label text not null,                                 -- for example 'Kolkata office'
    cidr cidr not null,                                  -- for example '203.0.113.0/24', or '203.0.113.7/32' for one address
    is_active boolean not null default true,
    created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------- 3. the daily record (one row per employee per day)
create table if not exists public.attendance (
    id bigint generated always as identity primary key,
    employee_id bigint not null references public.employee (id) on delete cascade,
    work_date date not null,                             -- the Indian date of the check-in
    check_in_at timestamptz not null,
    check_out_at timestamptz null,
    check_in_ip text null,
    check_out_ip text null,
    status text not null default 'checked_in'
        check (status in ('checked_in', 'present', 'half_day', 'absent')),
    is_late boolean not null default false,
    is_early_leave boolean not null default false,
    worked_minutes integer null,
    source text not null default 'self' check (source in ('self', 'correction')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint attendance_one_row_per_day unique (employee_id, work_date),
    constraint attendance_out_after_in check (check_out_at is null or check_out_at > check_in_at)
);
create index if not exists attendance_work_date_idx on public.attendance (work_date);

-- ---------------------------------------------------------------- 4. correction requests (missed check-in or check-out)
create table if not exists public.attendance_corrections (
    id bigint generated always as identity primary key,
    employee_id bigint not null references public.employee (id) on delete cascade,
    work_date date not null,
    requested_check_in timestamptz null,
    requested_check_out timestamptz null,
    reason text not null,
    status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
    decided_by bigint null references public.employee (id),
    decided_at timestamptz null,
    decision_note text null,                              -- 'Auto' when HR's own request was approved automatically
    created_at timestamptz not null default now(),
    constraint correction_has_a_time check (requested_check_in is not null or requested_check_out is not null)
);
-- Only one waiting request per employee per day.
create unique index if not exists attendance_one_pending_correction
    on public.attendance_corrections (employee_id, work_date) where status = 'pending';
create index if not exists attendance_corrections_employee_idx on public.attendance_corrections (employee_id, id desc);

alter table public.attendance_settings enable row level security;
alter table public.office_networks enable row level security;
alter table public.attendance enable row level security;
alter table public.attendance_corrections enable row level security;

notify pgrst, 'reload schema';
