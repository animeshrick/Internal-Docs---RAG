-- 12_create_superadmin.sql
-- Run once in the Supabase SQL editor, AFTER 11_superadmin.sql.
-- Creates the employee record for the super admin with every field filled. It is confidential,
-- so HR cannot look this person up in the chat.
-- Change the email, phone and date of birth first (the date of birth is used to build the temporary password).
-- Then, in the backend folder, run:   python scripts/create_credentials.py EMP-ADMIN superadmin

insert into public.employee
    (full_name, emp_id, email, phone, gender, dept, dob, doj, salary, full_current_address, emergency_ph_no, is_active, is_confidential)
values
    ('Super Admin', 'EMP-ADMIN', 'admin@your-company.com', '9000000000', 'Others', 'Management', '1990-01-01', current_date, 0,
     'Head office', '9000000001', true, true)
on conflict (emp_id) do nothing;
