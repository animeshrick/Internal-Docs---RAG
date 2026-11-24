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
    "id,emp_id,full_name,email,dept,dob,is_active,is_confidential,"
    "employee_auth(password_hash,role,must_change_password,failed_login_attempts,locked_until,password_changed_at,temp_password_expires_at,"
    "reset_ticket_hash,reset_ticket_expires_at),"
    # Only "is there a row?" - the government ID number itself is never read here. It is embedded in this
    # same request so that checking the token does not cost a second trip to the database.
    "employee_government_id(id_type,id_number_last4,submitted_at)"
)


def take_one_embedded_row(row, embedded_table_name):
    """
    Pull an embedded row out of a PostgREST answer.

    Supabase sends an embedded row as one dictionary or as a list, depending on the relationship,
    so this always gives back one dictionary or None, and removes the raw key from the row.
    """
    embedded = row.pop(embedded_table_name, None)
    if isinstance(embedded, list):
        embedded = embedded[0] if embedded else None
    return embedded


def find_employee_with_login(emp_id=None, employee_id=None, phone=None, phone_suffix=None):
    """
    Find one employee by emp_id (the login id), by the numeric database id, by exact phone number,
    or by phone_suffix - the last digits of a phone number, matched with a trailing wildcard.

    phone_suffix is what the WhatsApp reset uses: the sender arrives as +919876543210 but the
    stored number may or may not carry the country code, so matching the last ten digits is the
    only comparison that survives both spellings.
    Returns a dictionary with a "login" key (the employee_auth row, or None when no login exists yet).
    Returns None when the employee does not exist.
    """
    given = [(column, value) for column, value in
             (("emp_id", emp_id), ("id", employee_id), ("phone", phone), ("phone", phone_suffix))
             if value is not None]
    if len(given) != 1:
        raise ValueError("Give exactly one of emp_id, employee_id, phone or phone_suffix.")

    filter_column = given[0][0]
    if phone_suffix is not None:
        filter_expression = "like.*" + str(phone_suffix)
    else:
        filter_expression = "eq." + str(given[0][1])
    response = requests.get(
        get_table_url(EMPLOYEE_TABLE_NAME),
        headers=build_headers(),
        # The value goes in "params", so requests encodes it safely (no filter injection).
        params={"select": EMPLOYEE_WITH_LOGIN_COLUMNS, filter_column: filter_expression, "limit": 1},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Reading the employee")

    rows = response.json()
    if not rows:
        return None

    employee = rows[0]
    employee["login"] = take_one_embedded_row(employee, "employee_auth")
    employee["government_id"] = take_one_embedded_row(employee, "employee_government_id")
    return employee


def find_employee_by_email(email):
    """Find one employee by email (used before creating a new employee, to give a clear "already used" error)."""
    response = requests.get(
        get_table_url(EMPLOYEE_TABLE_NAME),
        headers=build_headers(),
        params={"select": "id,emp_id,email", "email": f"eq.{email}", "limit": 1},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Reading the employee")
    rows = response.json()
    return rows[0] if rows else None


def insert_employee(fields):
    """Create a brand-new employee row (no login yet). Returns the row Supabase saved, with its new id."""
    response = requests.post(
        get_table_url(EMPLOYEE_TABLE_NAME),
        headers=build_headers({"Prefer": "return=representation"}),
        json=fields,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Creating the employee")
    return response.json()[0]


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
