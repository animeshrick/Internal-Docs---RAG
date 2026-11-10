"""
chat/store.py - saves and reads the chat history in Supabase (table chat_messages, see sql/09_chat_history.sql).

Only the newest MESSAGES_KEPT_PER_EMPLOYEE messages of an employee are kept. The database deletes the
rest itself (trigger chat_messages_trim), so nothing here has to clean up and a bug or a crash in this
process cannot let the table grow.

Every function takes the employee id, so a person can only ever read their own messages.
"""
import requests

from app.core.supabase import REQUEST_TIMEOUT_SECONDS, build_headers, check_response, get_table_url, read_rows

CHAT_MESSAGES_TABLE_NAME = "chat_messages"
MESSAGES_KEPT_PER_EMPLOYEE = 10     # 5 questions and their 5 answers; the trigger in sql/09 enforces the same number


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
    """The newest messages of one employee, returned oldest first. before_id loads the page before it."""
    params = {"select": "id,sender,message,sources,created_at", "employee_id": f"eq.{employee_id}",
              "order": "id.desc", "limit": limit}
    if before_id is not None:
        params["id"] = f"lt.{before_id}"
    rows = read_rows(CHAT_MESSAGES_TABLE_NAME, params, "Reading the chat history")
    return list(reversed(rows))
