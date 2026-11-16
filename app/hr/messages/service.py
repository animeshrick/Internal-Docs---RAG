"""
hr/messages/service.py - 1-to-1 messaging rules: open (anyone may message anyone, including
admin/superadmin), a rolling 24-hour visibility window, and no edit/delete ever.

The 24-hour window is enforced here, at query time, on every read - never by a delete job, so
a message is never actually removed; it simply stops being returned once it falls outside the
window. "Read" tracking (for the unread badge) is a separate, best-effort marker per
conversation; it never hides or deletes anything either.
"""
from datetime import timedelta

from app.core import presence
from app.hr.messages import store as messages_store
from app.hr.people import store as people_store
from app.identity.auth import AuthError, get_current_time, parse_timestamp

VISIBILITY_WINDOW = timedelta(hours=24)
MESSAGE_BODY_MAX_LENGTH = 2000


def _window_start():
    return get_current_time() - VISIBILITY_WINDOW


def _people_by_id():
    return {row["id"]: row for row in people_store.list_active_people()}


def _find_person_by_emp_id(emp_id):
    for row in people_store.list_active_people():
        if row["emp_id"] == emp_id:
            return row
    return None


def _summary(row):
    return {"emp_id": row["emp_id"], "full_name": row["full_name"], "dept": row.get("dept"), "role": row.get("role"),
            "is_online": presence.is_online(row["id"])}


def list_people(viewer):
    """Everyone else, active - for the "new message" picker. Messaging is open: anyone may
    message anyone, including HR, the CEO and the super admin, so there is nothing to filter
    beyond "not myself"."""
    return [_summary(row) for row in people_store.list_active_people() if row["id"] != viewer["employee_id"]]


def _preview(row):
    """A short one-line summary of a message, for the conversation list."""
    if row["body"]:
        return row["body"] if len(row["body"]) <= 140 else row["body"][:140] + "..."
    return f"Sent a file: {row['attachment_filename']}" if row.get("attachment_filename") else "Sent a file"


def list_conversations(viewer):
    """Every conversation with at least one message in the last 24 hours, newest message first,
    each with an unread count (messages from that person since I last opened the thread)."""
    my_id = viewer["employee_id"]
    rows = messages_store.list_messages_for(my_id, _window_start().isoformat())  # already newest-first
    reads_by_partner = {row["other_employee_id"]: parse_timestamp(row["last_read_at"]) for row in messages_store.list_reads(my_id)}
    people = _people_by_id()

    conversations = {}
    for row in rows:
        partner_id = row["recipient_id"] if row["sender_id"] == my_id else row["sender_id"]
        conversation = conversations.get(partner_id)
        if conversation is None:
            partner = people.get(partner_id)
            conversation = {
                "person": _summary(partner) if partner else {"emp_id": None, "full_name": "(former employee)", "dept": None, "role": None, "is_online": False},
                "last_message": _preview(row),
                "last_message_at": row["created_at"],
                "unread_count": 0,
            }
            conversations[partner_id] = conversation
        if row["sender_id"] != my_id:  # a message they sent me counts toward my unread total
            last_read = reads_by_partner.get(partner_id)
            created_at = parse_timestamp(row["created_at"])
            if last_read is None or (created_at is not None and created_at > last_read):
                conversation["unread_count"] += 1

    return sorted(conversations.values(), key=lambda conversation: conversation["last_message_at"], reverse=True)


def unread_total(viewer):
    """The number shown on the "Messages" nav badge: every unread message, across every conversation."""
    return sum(conversation["unread_count"] for conversation in list_conversations(viewer))


def _message_view(row, my_id):
    return {
        "id": row["id"], "mine": row["sender_id"] == my_id, "body": row["body"],
        "attachment_path": row["attachment_path"], "attachment_filename": row["attachment_filename"],
        "attachment_content_type": row["attachment_content_type"], "created_at": row["created_at"],
    }


def get_thread(viewer, other_emp_id):
    """Every message with this one person from the last 24 hours, oldest first. Opening the
    thread marks it read, which is what clears the unread badge for this conversation."""
    other = _find_person_by_emp_id(other_emp_id)
    if other is None:
        raise AuthError(404, "That person was not found.")
    if other["id"] == viewer["employee_id"]:
        raise AuthError(400, "You cannot message yourself.")

    rows = messages_store.list_thread(viewer["employee_id"], other["id"], _window_start().isoformat())
    messages_store.mark_read(viewer["employee_id"], other["id"], get_current_time().isoformat())
    return {"person": _summary(other), "messages": [_message_view(row, viewer["employee_id"]) for row in rows]}


def send_message(viewer, other_emp_id, body, attachment=None):
    """attachment, if given, is {"path", "filename", "content_type"} from POST /messages/attachments."""
    other = _find_person_by_emp_id(other_emp_id)
    if other is None:
        raise AuthError(404, "That person was not found.")
    if other["id"] == viewer["employee_id"]:
        raise AuthError(400, "You cannot message yourself.")

    text = (body or "").strip() or None
    if text is None and attachment is None:
        raise AuthError(400, "Write a message or attach a file.")
    if text is not None and len(text) > MESSAGE_BODY_MAX_LENGTH:
        raise AuthError(400, f"Messages can be at most {MESSAGE_BODY_MAX_LENGTH} characters.")

    fields = {"sender_id": viewer["employee_id"], "recipient_id": other["id"], "body": text}
    if attachment:
        fields.update({
            "attachment_path": attachment["path"],
            "attachment_filename": attachment["filename"],
            "attachment_content_type": attachment["content_type"],
        })
    saved = messages_store.insert_message(fields)
    return _message_view(saved, viewer["employee_id"])


def check_can_read_attachment(viewer, message_id):
    """Returns the attachment's storage path if this person sent or received that message and it
    is still within the visibility window, else raises AuthError."""
    row = messages_store.find_message(message_id)
    if row is None:
        raise AuthError(404, "This message was not found.")
    my_id = viewer["employee_id"]
    if row["sender_id"] != my_id and row["recipient_id"] != my_id:
        raise AuthError(403, "You do not have access to this message.")
    if not row["attachment_path"]:
        raise AuthError(404, "This message has no attachment.")
    created_at = parse_timestamp(row["created_at"])
    if created_at is None or created_at < _window_start():
        raise AuthError(404, "This message is no longer available.")
    return row["attachment_path"]
