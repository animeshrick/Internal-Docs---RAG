-- 16_sick_leave_document_rule.sql
-- Run once in the Supabase SQL Editor, AFTER 04_leave_system.sql. Safe to run again.
--
-- What it adds:
--   A data-driven rule: a leave type can require a supporting document once a single request
--   goes above a number of days. Used for Sick Leave: more than 1 day needs a document; 1 day
--   or less (including a half day) can still be applied in the chat with no document, as before.
--
-- The application code (app/hr/leave/service.py, check_new_leave) reads this column and raises
-- the same "needs a supporting document" message used elsewhere, once days exceeds the threshold.

alter table public.leave_types add column if not exists require_document_above_days numeric null;

update public.leave_types set require_document_above_days = 1 where code = 'sick';

-- get_leave_balances() must also return the new column, so the website and the chat assistant
-- can see the threshold next to the balance. Re-created in full (same body as 04_leave_system.sql
-- plus the new column). Postgres refuses "create or replace" when the OUT-parameter row type
-- changes (adding a column counts as a change), so the old function is dropped first.
drop function if exists public.get_leave_balances(bigint, date);

create or replace function public.get_leave_balances(
    p_employee_id bigint,
    p_on_date date default current_date
)
returns table (
    type_code text,
    type_name text,
    limit_type text,
    days_allowed numeric,
    used_days numeric,
    pending_days numeric,
    remaining_days numeric,
    max_days_per_request numeric,
    can_apply_by_chat boolean,
    require_document_above_days numeric
)
language sql
stable
as $$
    with settings as (
        select 4 as leave_year_start_month
    ),
    leave_year as (
        select
            make_date(
                extract(year from p_on_date)::int
                    - case when extract(month from p_on_date) < s.leave_year_start_month then 1 else 0 end,
                s.leave_year_start_month,
                1
            ) as year_start
        from settings s
    )
    select
        t.code,
        t.name,
        t.limit_type,
        t.days_allowed,
        coalesce(sum(r.days) filter (where r.status = 'approved'), 0),
        coalesce(sum(r.days) filter (where r.status = 'pending'), 0),
        case
            when t.limit_type = 'per_year'
                then t.days_allowed - coalesce(sum(r.days) filter (where r.status in ('approved', 'pending')), 0)
            else null
        end,
        t.max_days_per_request,
        t.can_apply_by_chat,
        t.require_document_above_days
    from public.leave_types t
    join public.employee e on e.id = p_employee_id
    cross join leave_year y
    left join public.leave_requests r
        on r.employee_id = e.id
       and r.leave_type = t.code
       and r.status in ('approved', 'pending')
       and r.start_date >= y.year_start
       and r.start_date < (y.year_start + interval '1 year')::date
    where t.is_active
      and (t.applies_to_gender is null or t.applies_to_gender = e.gender)
    group by t.code, t.name, t.limit_type, t.days_allowed, t.max_days_per_request, t.can_apply_by_chat, t.require_document_above_days
    order by t.code;
$$;
