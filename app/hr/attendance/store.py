"""
hr/attendance/store.py - reads and writes the attendance tables in Supabase (sql/10_attendance.sql).

Same style as the other stores: plain "requests" calls to PostgREST. This file only talks to the database.
The attendance RULES (who may check in, what counts as late, who sees whom) are in service.py next to it.
"""
from datetime import datetime, timezone

import requests

from app.core.supabase import REQUEST_TIMEOUT_SECONDS, build_headers, check_response, flatten_role, get_table_url, read_rows

SETTINGS_TABLE_NAME = "attendance_settings"
OFFICE_NETWORKS_TABLE_NAME = "office_networks"
ATTENDANCE_TABLE_NAME = "attendance"
CORRECTIONS_TABLE_NAME = "attendance_corrections"

# One employee with the login role, which decides the attendance level.
PEOPLE_COLUMNS = "id,emp_id,full_name,dept,is_active,is_confidential,employee_auth(role)"


class AttendanceConflictError(Exception):
    """The database refused a second row for the same employee and day (or a second waiting correction)."""


def insert_row(table_name, fields, action_description):
    """Insert one row and return it. A 409 means the unique rule of the table was hit."""
    response = requests.post(get_table_url(table_name), headers=build_headers({"Prefer": "return=representation"}),
                             json=fields, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code == 409:
        raise AttendanceConflictError()
    check_response(response, action_description)
    return response.json()[0]


# ------------------------------------------------------------------ settings and networks
def get_settings():
    rows = read_rows(SETTINGS_TABLE_NAME, {"select": "*", "id": "eq.1", "limit": 1}, "Reading the attendance settings")
    if not rows:
        raise RuntimeError("The attendance settings row is missing. Run sql/10_attendance.sql.")
    return rows[0]


def update_settings(fields):
    """Change one or more settings (for example network_check_enabled). Always row id = 1."""
    response = requests.patch(get_table_url(SETTINGS_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                              params={"id": "eq.1"}, json=dict(fields, updated_at=datetime.now(timezone.utc).isoformat()),
                              timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Updating the attendance settings")
    return response.json()[0]


def list_active_office_networks():
    return read_rows(OFFICE_NETWORKS_TABLE_NAME, {"select": "id,label,cidr", "is_active": "eq.true"}, "Reading the office networks")


def list_all_office_networks():
    """Every office network, active or not, for the admin page that manages them."""
    return read_rows(OFFICE_NETWORKS_TABLE_NAME, {"select": "id,label,cidr,is_active", "order": "id.asc"}, "Reading the office networks")


def insert_office_network(fields):
    return insert_row(OFFICE_NETWORKS_TABLE_NAME, fields, "Saving the office network")


def update_office_network(network_id, fields):
    response = requests.patch(get_table_url(OFFICE_NETWORKS_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                              params={"id": f"eq.{network_id}"}, json=fields, timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Updating the office network")
    rows = response.json()
    return rows[0] if rows else None


# ------------------------------------------------------------------ people (with their login role)
def list_people():
    """Every active employee with a login role (an employee without a login has role None)."""
    rows = read_rows("employee", {"select": PEOPLE_COLUMNS, "is_active": "eq.true", "order": "emp_id.asc"}, "Reading the employees")
    return [flatten_role(row) for row in rows]


def get_person(employee_id):
    rows = read_rows("employee", {"select": PEOPLE_COLUMNS, "id": f"eq.{employee_id}", "limit": 1}, "Reading the employee")
    return flatten_role(rows[0]) if rows else None


# ------------------------------------------------------------------ holidays and leave (read only, for the calendar)
def list_holidays(start_date, end_date):
    return read_rows("holidays", {"select": "holiday_date,name", "and": f"(holiday_date.gte.{start_date},holiday_date.lte.{end_date})"},
                     "Reading the holidays")


def list_approved_leaves(employee_ids, start_date, end_date):
    """Approved leave that touches the date range, for the given employees."""
    if not employee_ids:
        return []
    id_list = ",".join(str(employee_id) for employee_id in employee_ids)
    return read_rows("leave_requests", {"select": "employee_id,leave_type,start_date,end_date,day_type", "status": "eq.approved",
                                        "employee_id": f"in.({id_list})", "start_date": f"lte.{end_date}", "end_date": f"gte.{start_date}"},
                     "Reading the approved leaves")


# ------------------------------------------------------------------ the daily record
def get_attendance(employee_id, work_date):
    rows = read_rows(ATTENDANCE_TABLE_NAME, {"select": "*", "employee_id": f"eq.{employee_id}", "work_date": f"eq.{work_date}", "limit": 1},
                     "Reading the attendance")
    return rows[0] if rows else None


def list_attendance(employee_ids, start_date, end_date):
    """Daily records of the given employees from start_date to end_date (both included)."""
    if not employee_ids:
        return []
    id_list = ",".join(str(employee_id) for employee_id in employee_ids)
    return read_rows(ATTENDANCE_TABLE_NAME, {"select": "*", "employee_id": f"in.({id_list})",
                                             "and": f"(work_date.gte.{start_date},work_date.lte.{end_date})", "order": "work_date.asc"},
                     "Reading the attendance")


def insert_attendance(fields):
    return insert_row(ATTENDANCE_TABLE_NAME, fields, "Saving the attendance")


def update_attendance(attendance_id, fields):
    response = requests.patch(get_table_url(ATTENDANCE_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                              params={"id": f"eq.{attendance_id}"}, json=dict(fields, updated_at=datetime.now(timezone.utc).isoformat()), timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Updating the attendance")
    return response.json()[0]


# ------------------------------------------------------------------ correction requests
def insert_correction(fields):
    return insert_row(CORRECTIONS_TABLE_NAME, fields, "Saving the correction request")


def get_correction(correction_id):
    rows = read_rows(CORRECTIONS_TABLE_NAME, {"select": "*", "id": f"eq.{correction_id}", "limit": 1}, "Reading the correction request")
    return rows[0] if rows else None


def list_corrections(employee_ids=None, status=None, limit=30):
    """Newest first. employee_ids=None means everyone (never used for a normal employee)."""
    params = {"select": "*", "order": "id.desc", "limit": limit}
    if employee_ids is not None:
        if not employee_ids:
            return []
        params["employee_id"] = f"in.({','.join(str(employee_id) for employee_id in employee_ids)})"
    if status:
        params["status"] = f"eq.{status}"
    return read_rows(CORRECTIONS_TABLE_NAME, params, "Reading the correction requests")


def update_correction(correction_id, fields, expected_status):
    """Change a request only if it still has the expected status. Returns the row, or None when someone else changed it first."""
    response = requests.patch(get_table_url(CORRECTIONS_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                              params={"id": f"eq.{correction_id}", "status": f"eq.{expected_status}"},
                              json=fields, timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Updating the correction request")
    rows = response.json()
    return rows[0] if rows else None
