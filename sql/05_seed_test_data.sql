-- 05_seed_test_data.sql
-- TEST DATA ONLY. Run after 03 and 04. Safe to run again (existing rows are kept).
-- Fictional people, so you can try login, leave and salary questions before real data exists.
-- Passwords are NOT stored here. Create the logins with:
--     python scripts/create_credentials.py EMP001 hr
--     python scripts/create_credentials.py EMP008 ceo
--     python scripts/create_credentials.py --all        (everyone else, as employee)

insert into public.employee
    (emp_id, full_name, email, phone, gender, dept, dob, doj, salary, is_active)
values
    ('EMP001', 'Priya Sen',        'priya.sen@onionorg.test',        '9000000001', 'Female', 'HR',          '1990-04-12', '2020-01-06',   85000, true),
    ('EMP002', 'Rahul Verma',      'rahul.verma@onionorg.test',      '9000000002', 'Male',   'IT',          '1988-09-23', '2019-03-18',  120000, true),
    ('EMP003', 'Ananya Das',       'ananya.das@onionorg.test',       '9000000003', 'Female', 'IT',          '1996-01-15', '2022-07-04',   70000, true),
    ('EMP004', 'Karan Mehta',      'karan.mehta@onionorg.test',      '9000000004', 'Male',   'Sales',       '1993-06-30', '2021-02-01',   60000, true),
    ('EMP005', 'Neha Kapoor',      'neha.kapoor@onionorg.test',      '9000000005', 'Female', 'Sales',       '1991-11-05', '2019-09-09',   95000, true),
    ('EMP006', 'Sourav Ghosh',     'sourav.ghosh@onionorg.test',     '9000000006', 'Male',   'Design',      '1997-02-18', '2023-01-16',   55000, true),
    ('EMP007', 'Meera Nair',       'meera.nair@onionorg.test',       '9000000007', 'Female', 'VideoEditor', '1998-08-09', '2023-05-22',   50000, true),
    ('EMP008', 'Parvati Chouhan',  'parvati.chouhan@onionorg.test',  '9000000008', 'Female', 'Management',  '1975-12-01', '2015-04-01', 7254150, true),
    ('EMP009', 'Old Employee',     'old.employee@onionorg.test',     '9000000009', 'Others', 'IT',          '1992-05-25', '2018-06-11',   65000, false)
on conflict (emp_id) do nothing;

-- Reporting managers (only filled where still empty).
update public.employee set manager_id = (select id from public.employee where emp_id = 'EMP002')
    where emp_id in ('EMP003', 'EMP006', 'EMP007') and manager_id is null;
update public.employee set manager_id = (select id from public.employee where emp_id = 'EMP005')
    where emp_id = 'EMP004' and manager_id is null;
update public.employee set manager_id = (select id from public.employee where emp_id = 'EMP008')
    where emp_id in ('EMP001', 'EMP002', 'EMP005') and manager_id is null;

-- Example leaves for Ananya Das (EMP003): 4 casual days approved and 1 pending.
-- With 8 casual days a year she has 8 - 4 - 1 = 3 left.
with seed (start_date, end_date, days, status, reason) as (
    values
        ('2026-05-04', '2026-05-06', 3, 'approved', 'Family function (test data)'),
        ('2026-06-15', '2026-06-15', 1, 'approved', 'Personal work (test data)'),
        ('2026-11-16', '2026-11-16', 1, 'pending',  'Bank work (test data)')
)
insert into public.leave_requests
    (employee_id, leave_type, start_date, end_date, day_type, days, reason, status, approver_id, decided_at)
select
    employee.id, 'casual', seed.start_date::date, seed.end_date::date, 'full', seed.days, seed.reason, seed.status,
    case when seed.status = 'approved' then manager.id end,
    case when seed.status = 'approved' then now() end
from seed
join public.employee employee on employee.emp_id = 'EMP003'
join public.employee manager on manager.emp_id = 'EMP002'
where not exists (
    select 1 from public.leave_requests existing
    where existing.employee_id = employee.id and existing.start_date = seed.start_date::date
);
