"""
hr/leave/store.py - reads and writes leave data in Supabase (leave_types, leave_requests, pending_actions ...).

Same style as the other stores: plain "requests" calls to PostgREST. This file only talks to the database.
The leave rules (how many days, who can approve ...) are in service.py next to it.
"""
import requests

from app.core.supabase import (REQUEST_TIMEOUT_SECONDS, build_headers, call_database_function, check_response,
                               get_table_url, read_rows)

EMPLOYEE_TABLE_NAME = "employee"
LEAVE_TYPES_TABLE_NAME = "leave_types"
LEAVE_REQUESTS_TABLE_NAME = "leave_requests"
PENDING_ACTIONS_TABLE_NAME = "pending_actions"

EMPLOYEE_COLUMNS = "id,emp_id,full_name,dept,gender,manager_id,is_active"
DEFAULT_NUMBER_OF_LEAVES_TO_LIST = 20


class LeaveOverlapError(Exception):
    """The database refused a leave because it overlaps another pending or approved leave of the same person."""


# ------------------------------------------------------------------ employees
def get_employee_row(employee_id):
    rows = read_rows(EMPLOYEE_TABLE_NAME, {"select": EMPLOYEE_COLUMNS, "id": f"eq.{employee_id}", "limit": 1},
                     "Reading the employee")
    return rows[0] if rows else None


def get_employees_by_ids(employee_ids):
    """Returns {database id: employee row}."""
    if not employee_ids:
        return {}
    id_list = ",".join(str(employee_id) for employee_id in sorted(set(employee_ids)))
    rows = read_rows(EMPLOYEE_TABLE_NAME, {"select": EMPLOYEE_COLUMNS, "id": f"in.({id_list})"}, "Reading employees")
    return {row["id"]: row for row in rows}


def list_direct_report_ids(manager_id):
    rows = read_rows(EMPLOYEE_TABLE_NAME, {"select": "id", "manager_id": f"eq.{manager_id}"}, "Reading the team")
    return [row["id"] for row in rows]


def list_unmanaged_employee_ids():
    """Employees whose manager_id is empty. Their leave is approved by the PM (or HR)."""
    rows = read_rows(EMPLOYEE_TABLE_NAME, {"select": "id", "manager_id": "is.null"}, "Reading employees without a manager")
    return [row["id"] for row in rows]


def list_active_employees_in_department(department_name):
    """Active employees of one department. ilike without wildcards is a case-insensitive exact match."""
    return read_rows(EMPLOYEE_TABLE_NAME, {"select": EMPLOYEE_COLUMNS, "dept": f"ilike.{department_name}", "is_active": "eq.true"},
                     "Reading the department")


# ------------------------------------------------------------------ leave types and day counting
def get_leave_types():
    """Returns {code: leave type row} for the active leave types."""
    rows = read_rows(LEAVE_TYPES_TABLE_NAME, {"select": "*", "is_active": "eq.true"}, "Reading leave types")
    return {row["code"]: row for row in rows}


def get_leave_balances(employee_id, on_date):
    """Used, pending and remaining days per leave type (database function get_leave_balances)."""
    return call_database_function("get_leave_balances", {"p_employee_id": employee_id, "p_on_date": on_date.isoformat()})


def count_leave_days(start_date, end_date, day_type, count_mode):
    """How many days a leave costs: skips Saturday, Sunday and holidays for working_days."""
    result = call_database_function("count_leave_days", {
        "p_start_date": start_date.isoformat(),
        "p_end_date": end_date.isoformat(),
        "p_day_type": day_type,
        "p_count_mode": count_mode,
    })
    return float(result)


# ------------------------------------------------------------------ leave requests
def find_overlapping_requests(employee_id, start_date, end_date):
    return read_rows(LEAVE_REQUESTS_TABLE_NAME, {
        "select": "id,leave_type,start_date,end_date,status",
        "employee_id": f"eq.{employee_id}",
        "status": "in.(pending,approved)",
        "start_date": f"lte.{end_date.isoformat()}",
        "end_date": f"gte.{start_date.isoformat()}",
    }, "Checking overlapping leaves")


def insert_leave_request(fields):
    """Save a new leave request and return the saved row."""
    response = requests.post(get_table_url(LEAVE_REQUESTS_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                             json=fields, timeout=REQUEST_TIMEOUT_SECONDS)
    # 409 = the database exclusion rule: two requests for the same dates arrived together.
    if response.status_code == 409:
        raise LeaveOverlapError()
    check_response(response, "Saving the leave request")
    return response.json()[0]


def get_leave_request(leave_id):
    rows = read_rows(LEAVE_REQUESTS_TABLE_NAME, {"select": "*", "id": f"eq.{leave_id}", "limit": 1}, "Reading the leave request")
    return rows[0] if rows else None


def list_leave_requests(employee_id, status=None, limit=DEFAULT_NUMBER_OF_LEAVES_TO_LIST):
    params = {"select": "*", "employee_id": f"eq.{employee_id}", "order": "start_date.desc", "limit": limit}
    if status:
        params["status"] = f"eq.{status}"
    return read_rows(LEAVE_REQUESTS_TABLE_NAME, params, "Reading your leaves")


def list_pending_requests(employee_ids=None):
    """Pending requests, oldest first. employee_ids=None means everyone (used for HR)."""
    params = {"select": "*", "status": "eq.pending", "order": "created_at.asc"}
    if employee_ids is not None:
        if not employee_ids:
            return []
        params["employee_id"] = f"in.({','.join(str(employee_id) for employee_id in employee_ids)})"
    return read_rows(LEAVE_REQUESTS_TABLE_NAME, params, "Reading pending leaves")


def update_leave_request(leave_id, fields, expected_status):
    """
    Change a leave request only if it still has the expected status.
    Returns the updated row, or None when someone else changed it first.
    """
    response = requests.patch(get_table_url(LEAVE_REQUESTS_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                              params={"id": f"eq.{leave_id}", "status": f"eq.{expected_status}"},
                              json=fields, timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Updating the leave request")
    rows = response.json()
    return rows[0] if rows else None


# ------------------------------------------------------------------ pending actions ("reply yes to confirm")
def get_waiting_action(employee_id):
    rows = read_rows(PENDING_ACTIONS_TABLE_NAME, {"select": "*", "employee_id": f"eq.{employee_id}", "status": "eq.waiting", "limit": 1},
                     "Reading the pending action")
    return rows[0] if rows else None


def get_action_by_id(action_id):
    rows = read_rows(PENDING_ACTIONS_TABLE_NAME, {"select": "*", "id": f"eq.{action_id}", "limit": 1}, "Reading the pending action")
    return rows[0] if rows else None


def update_action(action_id, fields, expected_status="waiting"):
    """Same idea as update_leave_request: only changes the action if it still has the expected status."""
    response = requests.patch(get_table_url(PENDING_ACTIONS_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                              params={"id": f"eq.{action_id}", "status": f"eq.{expected_status}"},
                              json=fields, timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Updating the pending action")
    rows = response.json()
    return rows[0] if rows else None


def create_pending_action(employee_id, action_type, payload, summary):
    """A new request replaces the old waiting one (the database allows only one waiting action per employee)."""
    old_action = get_waiting_action(employee_id)
    if old_action:
        update_action(old_action["id"], {"status": "cancelled"})
    response = requests.post(get_table_url(PENDING_ACTIONS_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                             json={"employee_id": employee_id, "action_type": action_type, "payload": payload, "summary": summary},
                             timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Saving the pending action")
    return response.json()[0]
