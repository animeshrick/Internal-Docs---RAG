"""
hr/payroll/store.py - reads salary data and payslips from Supabase (employee.salary, salary_components, payslips).
Only the database access is here. Who may see what is decided in service.py next to it.
"""
import requests

from app.core.supabase import REQUEST_TIMEOUT_SECONDS, build_headers, check_response, flatten_role, get_table_url, read_rows

SALARY_COLUMNS = "id,emp_id,full_name,dept,salary,employee_auth(role)"
CEO_NAMES = ("ceo", "the ceo", "chief executive officer")


def get_salary_details(employee_id):
    rows = read_rows("employee", {"select": SALARY_COLUMNS, "id": f"eq.{employee_id}", "limit": 1}, "Reading the salary")
    return flatten_role(rows[0]) if rows else None


def find_employee_by_text(person_text):
    """Find an employee from what HR typed: an emp_id, a full name, or "CEO". Returns salary details or None."""
    text = (person_text or "").strip().replace("*", "")
    if not text:
        return None
    if text.lower() in CEO_NAMES:
        ceo_logins = read_rows("employee_auth", {"select": "employee_id", "role": "eq.ceo", "limit": 1}, "Finding the CEO")
        return get_salary_details(ceo_logins[0]["employee_id"]) if ceo_logins else None
    for column, operator in (("emp_id", "eq"), ("full_name", "ilike")):
        rows = read_rows("employee", {"select": SALARY_COLUMNS, column: f"{operator}.{text}", "limit": 2}, "Finding the employee")
        if len(rows) == 1:
            return flatten_role(rows[0])
    return None


def list_salary_components(employee_id):
    return read_rows("salary_components", {"select": "component_name,component_type,monthly_amount", "employee_id": f"eq.{employee_id}",
                                           "order": "sort_order.asc,id.asc"}, "Reading the salary breakdown")


def get_payslip(employee_id, pay_month=None):
    """The payslip of one month (a date), or the newest one when pay_month is None."""
    params = {"select": "pay_month,net_pay,file_path", "employee_id": f"eq.{employee_id}", "order": "pay_month.desc", "limit": 1}
    if pay_month:
        params["pay_month"] = f"eq.{pay_month.isoformat()}"
    rows = read_rows("payslips", params, "Reading the payslip")
    return rows[0] if rows else None


def save_payslip(employee_id, pay_month, net_pay, file_path):
    """Add a payslip, or replace the file of an existing month."""
    response = requests.post(
        get_table_url("payslips"),
        headers=build_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        params={"on_conflict": "employee_id,pay_month"},
        json={"employee_id": employee_id, "pay_month": pay_month.isoformat(), "net_pay": net_pay, "file_path": file_path},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Saving the payslip")
