"""
hr/employees/store.py - reads and writes the "employee" and "employee_auth" tables in Supabase.

Same style as the other stores: plain "requests" calls to the Supabase REST API (PostgREST).
This file only talks to the database. The login rules (passwords, lockout, tokens) are in auth.py.
"""
import requests

from app.core.supabase import build_headers, check_response, get_table_url, REQUEST_TIMEOUT_SECONDS, ROWS_PER_PAGE

EMPLOYEE_TABLE_NAME = "employee"
AUTH_TABLE_NAME = "employee_auth"

# One request returns the employee row together with its login row (PostgREST calls this "embedding").
EMPLOYEE_WITH_LOGIN_COLUMNS = (
    "id,emp_id,full_name,email,dept,dob,is_active,"
    "employee_auth(password_hash,role,must_change_password,failed_login_attempts,locked_until,password_changed_at)"
)


def find_employee_with_login(emp_id=None, employee_id=None):
    """
    Find one employee by emp_id (the login id) OR by the numeric database id.
    Returns a dictionary with a "login" key (the employee_auth row, or None when no login exists yet).
    Returns None when the employee does not exist.
    """
    if (emp_id is None) == (employee_id is None):
        raise ValueError("Give exactly one of emp_id or employee_id.")

    filter_column, filter_value = ("emp_id", emp_id) if emp_id is not None else ("id", employee_id)
    response = requests.get(
        get_table_url(EMPLOYEE_TABLE_NAME),
        headers=build_headers(),
        # The value goes in "params", so requests encodes it safely (no filter injection).
        params={"select": EMPLOYEE_WITH_LOGIN_COLUMNS, filter_column: f"eq.{filter_value}", "limit": 1},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Reading the employee")

    rows = response.json()
    if not rows:
        return None

    employee = rows[0]
    login_data = employee.pop("employee_auth", None)
    # Supabase may send the login row as one dictionary or as a list. We always want one dictionary or None.
    if isinstance(login_data, list):
        login_data = login_data[0] if login_data else None
    employee["login"] = login_data
    return employee


def insert_login(employee_id, password_hash, role):
    """Create the login row for an employee."""
    response = requests.post(
        get_table_url(AUTH_TABLE_NAME),
        headers=build_headers({"Prefer": "return=minimal"}),
        json={"employee_id": employee_id, "password_hash": password_hash, "role": role},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Creating the login")


def update_login(employee_id, fields):
    """Change some columns of one login row. Example: update_login(5, {"failed_login_attempts": 2})"""
    response = requests.patch(
        get_table_url(AUTH_TABLE_NAME),
        headers=build_headers({"Prefer": "return=minimal"}),
        params={"employee_id": f"eq.{employee_id}"},
        json=fields,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Updating the login")


def fetch_all_rows(table_name, columns, order_column):
    """Read every row of a table, one page at a time (Supabase returns at most 1000 rows per request)."""
    all_rows = []
    offset = 0
    while True:
        response = requests.get(
            get_table_url(table_name),
            headers=build_headers(),
            params={"select": columns, "limit": ROWS_PER_PAGE, "offset": offset, "order": f"{order_column}.asc"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        check_response(response, f"Reading {table_name}")
        page = response.json()
        all_rows.extend(page)
        if len(page) < ROWS_PER_PAGE:
            return all_rows
        offset += ROWS_PER_PAGE


def list_employees_without_login():
    """Active employees that do not have a login yet (used by the bulk credentials script)."""
    employees = fetch_all_rows(EMPLOYEE_TABLE_NAME, "id,emp_id,full_name,is_active", "id")
    employee_ids_with_login = {row["employee_id"] for row in fetch_all_rows(AUTH_TABLE_NAME, "employee_id", "employee_id")}
    return [
        employee for employee in employees
        if employee["is_active"] and employee["id"] not in employee_ids_with_login
    ]
