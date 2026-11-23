"""
hr/government_id/store.py - reads and writes the "employee_government_id" table (sql/21_government_id.sql).

Same style as the other stores: plain "requests" calls to the Supabase REST API (PostgREST).
This file only talks to the database. The rules (which numbers are valid, who may look at one,
when an employee is still blocked) live in service.py.
"""
import requests

from app.core.supabase import build_headers, check_response, get_table_url, read_rows, REQUEST_TIMEOUT_SECONDS

GOVERNMENT_ID_TABLE_NAME = "employee_government_id"

# The encrypted number is never selected unless the super admin asks for one person on purpose,
# so an accidental "select *" somewhere cannot carry it out of the server.
SAFE_COLUMNS = "employee_id,id_type,id_number_last4,front_image_path,back_image_path,front_content_type,back_content_type,submitted_at"
FULL_COLUMNS = SAFE_COLUMNS + ",id_number_encrypted"


def get_government_id(employee_id, include_number=False):
    """One employee's stored ID, or None when they have not submitted yet."""
    columns = FULL_COLUMNS if include_number else SAFE_COLUMNS
    rows = read_rows(
        GOVERNMENT_ID_TABLE_NAME,
        {"select": columns, "employee_id": f"eq.{employee_id}", "limit": 1},
        "Reading the government ID",
    )
    return rows[0] if rows else None


def list_submitted_employee_ids():
    """The employee ids that already have an ID on file (used for the super admin's list)."""
    rows = read_rows(
        GOVERNMENT_ID_TABLE_NAME,
        {"select": "employee_id,id_type,id_number_last4,submitted_at"},
        "Reading the submitted government IDs",
    )
    return {row["employee_id"]: row for row in rows}


def insert_government_id(fields):
    """Save one employee's ID. The table's primary key makes a second row for the same person impossible."""
    response = requests.post(
        get_table_url(GOVERNMENT_ID_TABLE_NAME),
        headers=build_headers({"Prefer": "return=representation"}),
        json=fields,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Saving the government ID")
    return response.json()[0]


def delete_government_id(employee_id):
    """Remove one employee's ID row, so they are asked for it again at their next login."""
    response = requests.delete(
        get_table_url(GOVERNMENT_ID_TABLE_NAME),
        headers=build_headers(),
        params={"employee_id": f"eq.{employee_id}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Deleting the government ID")
