"""
chat_store.py - saves and reads the chat history in Supabase (table chat_messages, see sql/09_chat_history.sql).

Messages are kept for 30 days. Older ones are deleted (at most once an hour, when someone chats) and are never returned.
Every function takes the employee id, so a person can only ever read their own messages.
"""
import time
from datetime import datetime, timedelta, timezone

import requests

from app.core.supabase import REQUEST_TIMEOUT_SECONDS, build_headers, check_response, get_table_url, read_rows

CHAT_MESSAGES_TABLE_NAME = "chat_messages"
RETENTION_DAYS = 30
SECONDS_BETWEEN_CLEANUPS = 3600

last_cleanup_time = 0.0          # when the old messages were last deleted (seconds, from time.time())


def get_cutoff_text():
    """Messages created before this moment are too old."""
    return (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()


def delete_expired_messages():
    """Delete every message older than 30 days (for all employees)."""
    response = requests.delete(get_table_url(CHAT_MESSAGES_TABLE_NAME), headers=build_headers(),
                               params={"created_at": f"lt.{get_cutoff_text()}"}, timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Deleting old chat messages")


def delete_expired_messages_once_an_hour():
    global last_cleanup_time
    if time.time() - last_cleanup_time < SECONDS_BETWEEN_CLEANUPS:
        return
    last_cleanup_time = time.time()
    delete_expired_messages()


def save_exchange(employee_id, question_text, asked_at, answer_text, sources, answered_at):
    """Save the question and its answer. The times are datetime objects. Returns the two saved rows (question, answer)."""
    rows_to_save = [
        {"employee_id": employee_id, "sender": "user", "message": question_text, "sources": [], "created_at": asked_at.isoformat()},
        {"employee_id": employee_id, "sender": "assistant", "message": answer_text, "sources": sources or [], "created_at": answered_at.isoformat()},
    ]
    response = requests.post(get_table_url(CHAT_MESSAGES_TABLE_NAME), headers=build_headers({"Prefer": "return=representation"}),
                             json=rows_to_save, timeout=REQUEST_TIMEOUT_SECONDS)
    check_response(response, "Saving the chat messages")
    saved_rows = sorted(response.json(), key=lambda row: row["id"])
    return saved_rows[0], saved_rows[1]


def list_messages(employee_id, limit, before_id=None):
    """The newest messages of one employee (not older than 30 days), returned oldest first. before_id loads the page before it."""
    params = {"select": "id,sender,message,sources,created_at", "employee_id": f"eq.{employee_id}",
              "created_at": f"gte.{get_cutoff_text()}", "order": "id.desc", "limit": limit}
    if before_id is not None:
        params["id"] = f"lt.{before_id}"
    rows = read_rows(CHAT_MESSAGES_TABLE_NAME, params, "Reading the chat history")
    return list(reversed(rows))
