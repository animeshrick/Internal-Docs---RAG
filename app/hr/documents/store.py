"""
hr/documents/store.py - reads and writes "shared_documents" and "document_access_requests".

Same style as the other stores: plain "requests" calls to the Supabase REST API (PostgREST).
The business rules (who may see what, how long access lasts) live in service.py.
"""
import requests

from app.core.supabase import build_headers, check_response, get_table_url, read_rows, REQUEST_TIMEOUT_SECONDS

DOCUMENTS_TABLE_NAME = "shared_documents"
REQUESTS_TABLE_NAME = "document_access_requests"

DOCUMENT_COLUMNS = "id,owner_id,original_filename,storage_path,content_type,size_bytes,created_at,employee(emp_id,full_name)"
REQUEST_COLUMNS = "id,document_id,requester_id,status,requested_at,decided_at,expires_at"
REQUEST_WITH_DOCUMENT_COLUMNS = REQUEST_COLUMNS + ",shared_documents(id,original_filename,owner_id)"
REQUEST_WITH_REQUESTER_COLUMNS = REQUEST_COLUMNS + ",employee(emp_id,full_name)"


def list_documents():
    """Every uploaded document, newest first, with the owner's name attached."""
    return read_rows(DOCUMENTS_TABLE_NAME, {"select": DOCUMENT_COLUMNS, "order": "id.desc"}, "Reading the shared documents")


def get_document(document_id):
    rows = read_rows(DOCUMENTS_TABLE_NAME, {"select": DOCUMENT_COLUMNS, "id": f"eq.{document_id}", "limit": 1}, "Reading the shared document")
    return rows[0] if rows else None


def insert_document(fields):
    response = requests.post(
        get_table_url(DOCUMENTS_TABLE_NAME),
        headers=build_headers({"Prefer": "return=representation"}),
        json=fields,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Saving the document")
    return response.json()[0]


def delete_document(document_id):
    response = requests.delete(
        get_table_url(DOCUMENTS_TABLE_NAME),
        headers=build_headers(),
        params={"id": f"eq.{document_id}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Deleting the document")


def list_requests_for_document(document_id):
    """Every request made for one document (any status), with the requester's name attached. Owner only."""
    return read_rows(
        REQUESTS_TABLE_NAME,
        {"select": REQUEST_WITH_REQUESTER_COLUMNS, "document_id": f"eq.{document_id}", "order": "id.desc"},
        "Reading the document's access requests",
    )


def list_requests_by_requester(requester_id):
    """Every request this person made, with the document's name attached."""
    return read_rows(
        REQUESTS_TABLE_NAME,
        {"select": REQUEST_WITH_DOCUMENT_COLUMNS, "requester_id": f"eq.{requester_id}", "order": "id.desc"},
        "Reading your access requests",
    )


def list_pending_requests():
    """Every request still waiting for a decision, with the document's name and owner attached."""
    return read_rows(
        REQUESTS_TABLE_NAME,
        {"select": REQUEST_WITH_DOCUMENT_COLUMNS + ",employee(emp_id,full_name)", "status": "eq.pending", "order": "id.asc"},
        "Reading the waiting access requests",
    )


def find_request(request_id):
    rows = read_rows(
        REQUESTS_TABLE_NAME,
        {"select": REQUEST_WITH_DOCUMENT_COLUMNS, "id": f"eq.{request_id}", "limit": 1},
        "Reading the access request",
    )
    return rows[0] if rows else None


def insert_request(fields):
    response = requests.post(
        get_table_url(REQUESTS_TABLE_NAME),
        headers=build_headers({"Prefer": "return=representation"}),
        json=fields,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Saving the access request")
    return response.json()[0]


def update_request(request_id, fields):
    response = requests.patch(
        get_table_url(REQUESTS_TABLE_NAME),
        headers=build_headers({"Prefer": "return=representation"}),
        params={"id": f"eq.{request_id}"},
        json=fields,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Updating the access request")
    rows = response.json()
    return rows[0] if rows else None
