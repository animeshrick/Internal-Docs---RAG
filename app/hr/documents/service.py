"""
hr/documents/service.py - who may see which shared document, and for how long.

Everybody sees every document's NAME. Only the owner can open the file itself, until the
owner approves someone else's request; that approval is read-only and lasts a fixed number
of days, after which the person must ask again.
"""
from datetime import timedelta

from app.hr.documents import store as documents_store
from app.identity.auth import AuthError, get_current_time, parse_timestamp

ACCESS_DURATION_DAYS = 10


def _employee_summary(row):
    employee = row.get("employee")
    if isinstance(employee, list):
        employee = employee[0] if employee else None
    return {"emp_id": employee["emp_id"], "full_name": employee["full_name"]} if employee else None


def _has_active_access(request_row):
    if request_row["status"] != "approved":
        return False
    expires_at = parse_timestamp(request_row.get("expires_at"))
    return expires_at is not None and expires_at > get_current_time()


def _my_status(document_id, my_requests_by_document):
    """None, "pending", "approved" (with expires_at) or "denied", from this viewer's own requests only."""
    request_row = my_requests_by_document.get(document_id)
    if request_row is None:
        return {"state": "none"}
    if request_row["status"] == "pending":
        return {"state": "pending", "requested_at": request_row["requested_at"]}
    if _has_active_access(request_row):
        return {"state": "approved", "expires_at": request_row["expires_at"]}
    return {"state": "denied" if request_row["status"] == "denied" else "none"}


def list_documents(viewer):
    """Every document's name and owner, plus this viewer's own access state for each."""
    documents = documents_store.list_documents()
    my_requests = documents_store.list_requests_by_requester(viewer["employee_id"])
    # Only the most recent request per document decides the state shown.
    my_requests_by_document = {}
    for request_row in my_requests:
        my_requests_by_document.setdefault(request_row["document_id"], request_row)

    result = []
    for row in documents:
        is_owner = row["owner_id"] == viewer["employee_id"]
        result.append({
            "id": row["id"], "original_filename": row["original_filename"], "content_type": row["content_type"],
            "size_bytes": row["size_bytes"], "created_at": row["created_at"],
            "owner": _employee_summary(row), "is_owner": is_owner,
            "my_access": {"state": "owner"} if is_owner else _my_status(row["id"], my_requests_by_document),
        })
    return result


def _load_document_or_404(document_id):
    document = documents_store.get_document(document_id)
    if document is None:
        raise AuthError(404, "This document was not found.")
    return document


def get_document_detail(viewer, document_id):
    """Everyone gets the owner's name and their own access state. The owner also gets the full request list."""
    document = _load_document_or_404(document_id)
    is_owner = document["owner_id"] == viewer["employee_id"]

    detail = {
        "id": document["id"], "original_filename": document["original_filename"], "content_type": document["content_type"],
        "size_bytes": document["size_bytes"], "created_at": document["created_at"],
        "owner": _employee_summary(document), "is_owner": is_owner,
    }
    if is_owner:
        requests_rows = documents_store.list_requests_for_document(document_id)
        detail["requests"] = [
            {
                "id": row["id"], "requester": _employee_summary(row), "status": row["status"],
                "requested_at": row["requested_at"], "decided_at": row["decided_at"], "expires_at": row["expires_at"],
                "is_active": _has_active_access(row),
            }
            for row in requests_rows
        ]
    else:
        my_requests = documents_store.list_requests_by_requester(viewer["employee_id"])
        my_requests_by_document = {}
        for request_row in my_requests:
            my_requests_by_document.setdefault(request_row["document_id"], request_row)
        detail["my_access"] = _my_status(document_id, my_requests_by_document)
    return detail


def request_access(viewer, document_id):
    """Ask the owner for read-only access. Raises AuthError if that makes no sense right now."""
    document = _load_document_or_404(document_id)
    if document["owner_id"] == viewer["employee_id"]:
        raise AuthError(400, "This is your own document.")

    my_requests = documents_store.list_requests_by_requester(viewer["employee_id"])
    existing = next((row for row in my_requests if row["document_id"] == document_id), None)
    if existing is not None:
        if existing["status"] == "pending":
            raise AuthError(409, "You already asked for this document. Wait for the owner's decision.")
        if _has_active_access(existing):
            raise AuthError(409, "You already have access to this document.")

    return documents_store.insert_request({
        "document_id": document_id, "requester_id": viewer["employee_id"], "status": "pending",
    })


def list_my_pending_inbox(owner):
    """Requests waiting for a decision, across every document THIS person owns."""
    pending = documents_store.list_pending_requests()
    mine = [row for row in pending if row["shared_documents"]["owner_id"] == owner["employee_id"]]
    return [
        {
            "id": row["id"], "document": {"id": row["shared_documents"]["id"], "original_filename": row["shared_documents"]["original_filename"]},
            "requester": _employee_summary(row), "requested_at": row["requested_at"],
        }
        for row in mine
    ]


def list_my_requests(viewer):
    """What happened to every request THIS person made (for a quick "did they decide yet" check)."""
    rows = documents_store.list_requests_by_requester(viewer["employee_id"])
    return [
        {
            "id": row["id"], "document": {"id": row["shared_documents"]["id"], "original_filename": row["shared_documents"]["original_filename"]},
            "status": row["status"], "requested_at": row["requested_at"], "decided_at": row["decided_at"],
            "expires_at": row["expires_at"], "is_active": _has_active_access(row),
        }
        for row in rows
    ]


def decide_request(owner, request_id, decision):
    """The owner approves or denies one request waiting for them."""
    request_row = documents_store.find_request(request_id)
    if request_row is None:
        raise AuthError(404, "This request was not found.")
    if request_row["shared_documents"]["owner_id"] != owner["employee_id"]:
        raise AuthError(403, "Only the owner of that document can decide this.")
    if request_row["status"] != "pending":
        raise AuthError(409, "This request was already decided.")

    now = get_current_time()
    fields = {"status": decision, "decided_at": now.isoformat()}
    fields["expires_at"] = (now + timedelta(days=ACCESS_DURATION_DAYS)).isoformat() if decision == "approved" else None
    return documents_store.update_request(request_id, fields)


def check_can_read(viewer, document_id):
    """Returns the document's storage path if this person may read it right now, else raises AuthError."""
    document = _load_document_or_404(document_id)
    if document["owner_id"] == viewer["employee_id"]:
        return document["storage_path"]

    my_requests = documents_store.list_requests_by_requester(viewer["employee_id"])
    request_row = next((row for row in my_requests if row["document_id"] == document_id), None)
    if request_row is None or not _has_active_access(request_row):
        raise AuthError(403, "You do not have access to this document. Ask the owner for access first.")
    return document["storage_path"]


def check_can_delete(owner, document_id):
    """Returns the document (with its storage path) if this person may delete it, else raises AuthError."""
    document = _load_document_or_404(document_id)
    if document["owner_id"] != owner["employee_id"]:
        raise AuthError(403, "Only the owner can delete this document.")
    return document
