"""
hr/reports/store.py - the WhatsApp numbers that receive the twice-daily HR report
(sql/18_whatsapp_report_numbers.sql).

Same style as the other stores: plain "requests" calls to PostgREST. Who may manage this list,
and the 10-number limit, are rules - they live in service.py next to it.
"""
import requests

from app.core.supabase import REQUEST_TIMEOUT_SECONDS, build_headers, check_response, get_table_url, read_rows

TABLE_NAME = "whatsapp_report_numbers"
COLUMNS = "id,label,phone_number,created_at"


def list_numbers():
    """Every number, oldest first (so the list order stays stable as numbers are added or removed)."""
    return read_rows(TABLE_NAME, {"select": COLUMNS, "order": "id.asc"}, "Reading the WhatsApp report numbers")


def insert_number(label, phone_number):
    """Add one number. Returns None instead of raising when the number is already on the list
    (the table's unique constraint on phone_number) - service.py turns that into a clear message."""
    response = requests.post(get_table_url(TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                             json={"label": label, "phone_number": phone_number}, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code == 409:
        return None
    check_response(response, "Saving the WhatsApp number")
    return response.json()[0]


def delete_number(number_id):
    """Remove one number. Returns the removed row, or None if there was no such number."""
    response = requests.delete(get_table_url(TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                               params={"id": f"eq.{number_id}"}, timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Removing the WhatsApp number")
    rows = response.json()
    return rows[0] if rows else None
