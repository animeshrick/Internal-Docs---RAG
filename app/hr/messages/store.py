"""
hr/messages/store.py - reads and writes "direct_messages" and "direct_message_reads".

Same style as the other stores: plain "requests" calls to the Supabase REST API (PostgREST).
The business rules (the 24-hour visibility window, who an unread count belongs to) live in
service.py next to it - this file only knows column names and filters.
"""
import requests

from app.core.supabase import build_headers, check_response, get_table_url, read_rows, REQUEST_TIMEOUT_SECONDS

MESSAGES_TABLE_NAME = "direct_messages"
READS_TABLE_NAME = "direct_message_reads"

MESSAGE_COLUMNS = "id,sender_id,recipient_id,body,attachment_path,attachment_filename,attachment_content_type,created_at"


def list_messages_for(employee_id, since_iso):
    """Every message to or from this person, newest first, from "since_iso" onward. Used to build
    the conversation list (one row per other person) and the unread counts."""
    params = {
        "select": MESSAGE_COLUMNS,
        "or": f"(sender_id.eq.{employee_id},recipient_id.eq.{employee_id})",
        "created_at": f"gte.{since_iso}",
        "order": "created_at.desc",
    }
    return read_rows(MESSAGES_TABLE_NAME, params, "Reading your messages")


def list_thread(employee_id, other_employee_id, since_iso):
    """Every message between exactly these two people, from "since_iso" onward, oldest first."""
    params = {
        "select": MESSAGE_COLUMNS,
        "or": f"(and(sender_id.eq.{employee_id},recipient_id.eq.{other_employee_id}),"
              f"and(sender_id.eq.{other_employee_id},recipient_id.eq.{employee_id}))",
        "created_at": f"gte.{since_iso}",
        "order": "created_at.asc",
    }
    return read_rows(MESSAGES_TABLE_NAME, params, "Reading the conversation")


def find_message(message_id):
    rows = read_rows(MESSAGES_TABLE_NAME, {"select": MESSAGE_COLUMNS, "id": f"eq.{message_id}", "limit": 1}, "Reading the message")
    return rows[0] if rows else None


def insert_message(fields):
    response = requests.post(
        get_table_url(MESSAGES_TABLE_NAME),
        headers=build_headers({"Prefer": "return=representation"}),
        json=fields,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Sending the message")
    return response.json()[0]


def list_reads(employee_id):
    """Every conversation this person has a "last read" marker for (one row per other person)."""
    return read_rows(
        READS_TABLE_NAME,
        {"select": "other_employee_id,last_read_at", "employee_id": f"eq.{employee_id}"},
        "Reading your read receipts",
    )


def mark_read(employee_id, other_employee_id, when_iso):
    """Upsert the "I have read this conversation up to here" marker (insert the first time,
    overwrite after, keyed on the (employee_id, other_employee_id) primary key)."""
    response = requests.post(
        get_table_url(READS_TABLE_NAME),
        headers=build_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json={"employee_id": employee_id, "other_employee_id": other_employee_id, "last_read_at": when_iso},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Marking the conversation read")
