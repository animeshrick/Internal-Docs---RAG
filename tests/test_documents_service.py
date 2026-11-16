"""Tests for app/hr/documents/service.py. A fake in-memory store replaces Supabase."""
from datetime import timedelta

import pytest

from app.hr.documents import service as documents_service
from app.hr.documents import store as documents_store
from app.identity.auth import AuthError, get_current_time


def employee(employee_id, emp_id, full_name="Someone"):
    return {"employee_id": employee_id, "emp_id": emp_id, "full_name": full_name}


class FakeStore:
    """Behaves like documents_store, but keeps everything in memory."""

    def __init__(self):
        self.documents = {}
        self.requests = {}
        self.next_document_id = 1
        self.next_request_id = 1

    def _employee_blob(self, employee_id):
        names = {1: "Owner One", 2: "Requester Two", 3: "Requester Three"}
        emp_ids = {1: "EMP001", 2: "EMP002", 3: "EMP003"}
        return {"emp_id": emp_ids.get(employee_id, f"EMP{employee_id}"), "full_name": names.get(employee_id, f"Person {employee_id}")}

    def seed_document(self, owner_id=1, original_filename="report.pdf", storage_path="1/abc-report.pdf"):
        document_id = self.next_document_id
        self.next_document_id += 1
        self.documents[document_id] = {
            "id": document_id, "owner_id": owner_id, "original_filename": original_filename,
            "storage_path": storage_path, "content_type": "application/pdf", "size_bytes": 100,
            "created_at": "2026-01-01T00:00:00+00:00", "employee": self._employee_blob(owner_id),
        }
        return document_id

    def seed_request(self, document_id, requester_id, status="pending", expires_at=None, decided_at=None):
        request_id = self.next_request_id
        self.next_request_id += 1
        self.requests[request_id] = {
            "id": request_id, "document_id": document_id, "requester_id": requester_id, "status": status,
            "requested_at": "2026-01-01T00:00:00+00:00", "decided_at": decided_at, "expires_at": expires_at,
            "employee": self._employee_blob(requester_id),
        }
        return request_id

    # --- store-shaped methods ---
    def list_documents(self):
        return [dict(row) for row in sorted(self.documents.values(), key=lambda row: -row["id"])]

    def get_document(self, document_id):
        row = self.documents.get(document_id)
        return dict(row) if row else None

    def insert_document(self, fields):
        document_id = self.next_document_id
        self.next_document_id += 1
        row = dict(fields, id=document_id)
        self.documents[document_id] = row
        return row

    def delete_document(self, document_id):
        self.documents.pop(document_id, None)

    def list_requests_for_document(self, document_id):
        return [dict(row) for row in self.requests.values() if row["document_id"] == document_id]

    def list_requests_by_requester(self, requester_id):
        rows = [row for row in self.requests.values() if row["requester_id"] == requester_id]
        result = []
        for row in sorted(rows, key=lambda row: -row["id"]):
            document = self.documents.get(row["document_id"], {})
            result.append(dict(row, shared_documents={"id": document.get("id"), "original_filename": document.get("original_filename"), "owner_id": document.get("owner_id")}))
        return result

    def list_pending_requests(self):
        rows = [row for row in self.requests.values() if row["status"] == "pending"]
        result = []
        for row in sorted(rows, key=lambda row: row["id"]):
            document = self.documents.get(row["document_id"], {})
            result.append(dict(row, shared_documents={"id": document.get("id"), "original_filename": document.get("original_filename"), "owner_id": document.get("owner_id")}))
        return result

    def find_request(self, request_id):
        row = self.requests.get(request_id)
        if row is None:
            return None
        document = self.documents.get(row["document_id"], {})
        return dict(row, shared_documents={"id": document.get("id"), "original_filename": document.get("original_filename"), "owner_id": document.get("owner_id")})

    def insert_request(self, fields):
        request_id = self.next_request_id
        self.next_request_id += 1
        row = dict(fields, id=request_id, requested_at="2026-01-01T00:00:00+00:00", decided_at=None, expires_at=None)
        self.requests[request_id] = row
        return row

    def update_request(self, request_id, fields):
        row = self.requests.get(request_id)
        if row is None:
            return None
        row.update(fields)
        return dict(row)


@pytest.fixture
def store(monkeypatch):
    fake_store = FakeStore()
    monkeypatch.setattr(documents_store, "list_documents", fake_store.list_documents)
    monkeypatch.setattr(documents_store, "get_document", fake_store.get_document)
    monkeypatch.setattr(documents_store, "insert_document", fake_store.insert_document)
    monkeypatch.setattr(documents_store, "delete_document", fake_store.delete_document)
    monkeypatch.setattr(documents_store, "list_requests_for_document", fake_store.list_requests_for_document)
    monkeypatch.setattr(documents_store, "list_requests_by_requester", fake_store.list_requests_by_requester)
    monkeypatch.setattr(documents_store, "list_pending_requests", fake_store.list_pending_requests)
    monkeypatch.setattr(documents_store, "find_request", fake_store.find_request)
    monkeypatch.setattr(documents_store, "insert_request", fake_store.insert_request)
    monkeypatch.setattr(documents_store, "update_request", fake_store.update_request)
    return fake_store


def test_list_documents_shows_owner_state_for_the_owners_own_file(store):
    document_id = store.seed_document(owner_id=1)
    result = documents_service.list_documents(employee(1, "EMP001"))
    assert result[0]["id"] == document_id
    assert result[0]["is_owner"] is True
    assert result[0]["my_access"] == {"state": "owner"}


def test_list_documents_shows_none_state_for_a_stranger_with_no_request(store):
    store.seed_document(owner_id=1)
    result = documents_service.list_documents(employee(2, "EMP002"))
    assert result[0]["is_owner"] is False
    assert result[0]["my_access"] == {"state": "none"}


def test_list_documents_shows_pending_state_after_a_request(store):
    document_id = store.seed_document(owner_id=1)
    store.seed_request(document_id, requester_id=2, status="pending")
    result = documents_service.list_documents(employee(2, "EMP002"))
    assert result[0]["my_access"]["state"] == "pending"


def test_list_documents_shows_approved_state_while_not_expired(store):
    document_id = store.seed_document(owner_id=1)
    future = (get_current_time() + timedelta(days=5)).isoformat()
    store.seed_request(document_id, requester_id=2, status="approved", expires_at=future)
    result = documents_service.list_documents(employee(2, "EMP002"))
    assert result[0]["my_access"]["state"] == "approved"


def test_list_documents_shows_none_state_once_approval_expired(store):
    document_id = store.seed_document(owner_id=1)
    past = (get_current_time() - timedelta(days=1)).isoformat()
    store.seed_request(document_id, requester_id=2, status="approved", expires_at=past)
    result = documents_service.list_documents(employee(2, "EMP002"))
    assert result[0]["my_access"]["state"] == "none"


def test_get_document_detail_missing_document_raises_404(store):
    with pytest.raises(AuthError) as error:
        documents_service.get_document_detail(employee(1, "EMP001"), 999)
    assert error.value.status_code == 404


def test_get_document_detail_gives_the_owner_the_request_list(store):
    document_id = store.seed_document(owner_id=1)
    store.seed_request(document_id, requester_id=2, status="pending")
    detail = documents_service.get_document_detail(employee(1, "EMP001"), document_id)
    assert detail["is_owner"] is True
    assert len(detail["requests"]) == 1
    assert detail["requests"][0]["requester"]["emp_id"] == "EMP002"


def test_get_document_detail_gives_a_non_owner_their_own_access_state(store):
    document_id = store.seed_document(owner_id=1)
    detail = documents_service.get_document_detail(employee(2, "EMP002"), document_id)
    assert detail["is_owner"] is False
    assert detail["my_access"] == {"state": "none"}


def test_request_access_creates_a_pending_request(store):
    document_id = store.seed_document(owner_id=1)
    result = documents_service.request_access(employee(2, "EMP002"), document_id)
    assert result["status"] == "pending"
    assert result["requester_id"] == 2


def test_request_access_refuses_for_your_own_document(store):
    document_id = store.seed_document(owner_id=1)
    with pytest.raises(AuthError) as error:
        documents_service.request_access(employee(1, "EMP001"), document_id)
    assert error.value.status_code == 400


def test_request_access_refuses_a_second_pending_request(store):
    document_id = store.seed_document(owner_id=1)
    documents_service.request_access(employee(2, "EMP002"), document_id)
    with pytest.raises(AuthError) as error:
        documents_service.request_access(employee(2, "EMP002"), document_id)
    assert error.value.status_code == 409


def test_request_access_refuses_when_already_has_active_access(store):
    document_id = store.seed_document(owner_id=1)
    future = (get_current_time() + timedelta(days=5)).isoformat()
    store.seed_request(document_id, requester_id=2, status="approved", expires_at=future)
    with pytest.raises(AuthError) as error:
        documents_service.request_access(employee(2, "EMP002"), document_id)
    assert error.value.status_code == 409


def test_request_access_allowed_again_after_a_denial(store):
    document_id = store.seed_document(owner_id=1)
    store.seed_request(document_id, requester_id=2, status="denied", decided_at="2026-01-01T00:00:00+00:00")
    result = documents_service.request_access(employee(2, "EMP002"), document_id)
    assert result["status"] == "pending"


def test_list_my_pending_inbox_only_shows_requests_for_documents_i_own(store):
    document_1 = store.seed_document(owner_id=1)
    document_2 = store.seed_document(owner_id=3)
    store.seed_request(document_1, requester_id=2, status="pending")
    store.seed_request(document_2, requester_id=2, status="pending")
    inbox = documents_service.list_my_pending_inbox(employee(1, "EMP001"))
    assert len(inbox) == 1
    assert inbox[0]["document"]["id"] == document_1


def test_list_my_requests_reports_my_own_outcomes(store):
    document_id = store.seed_document(owner_id=1)
    store.seed_request(document_id, requester_id=2, status="denied", decided_at="2026-01-01T00:00:00+00:00")
    mine = documents_service.list_my_requests(employee(2, "EMP002"))
    assert mine[0]["status"] == "denied"
    assert mine[0]["is_active"] is False


def test_decide_request_approves_and_sets_a_ten_day_expiry(store):
    document_id = store.seed_document(owner_id=1)
    request_id = store.seed_request(document_id, requester_id=2, status="pending")
    before = get_current_time()
    updated = documents_service.decide_request(employee(1, "EMP001"), request_id, "approved")
    assert updated["status"] == "approved"
    from app.identity.auth import parse_timestamp
    expires_at = parse_timestamp(updated["expires_at"])
    assert (expires_at - before).days in (9, 10)


def test_decide_request_denies_with_no_expiry(store):
    document_id = store.seed_document(owner_id=1)
    request_id = store.seed_request(document_id, requester_id=2, status="pending")
    updated = documents_service.decide_request(employee(1, "EMP001"), request_id, "denied")
    assert updated["status"] == "denied"
    assert updated["expires_at"] is None


def test_decide_request_refuses_for_a_non_owner(store):
    document_id = store.seed_document(owner_id=1)
    request_id = store.seed_request(document_id, requester_id=2, status="pending")
    with pytest.raises(AuthError) as error:
        documents_service.decide_request(employee(3, "EMP003"), request_id, "approved")
    assert error.value.status_code == 403


def test_decide_request_refuses_a_request_already_decided(store):
    document_id = store.seed_document(owner_id=1)
    request_id = store.seed_request(document_id, requester_id=2, status="approved", decided_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(AuthError) as error:
        documents_service.decide_request(employee(1, "EMP001"), request_id, "denied")
    assert error.value.status_code == 409


def test_decide_request_missing_request_raises_404(store):
    with pytest.raises(AuthError) as error:
        documents_service.decide_request(employee(1, "EMP001"), 999, "approved")
    assert error.value.status_code == 404


def test_check_can_read_allows_the_owner(store):
    document_id = store.seed_document(owner_id=1, storage_path="1/x.pdf")
    assert documents_service.check_can_read(employee(1, "EMP001"), document_id) == "1/x.pdf"


def test_check_can_read_allows_someone_with_active_approval(store):
    document_id = store.seed_document(owner_id=1, storage_path="1/x.pdf")
    future = (get_current_time() + timedelta(days=5)).isoformat()
    store.seed_request(document_id, requester_id=2, status="approved", expires_at=future)
    assert documents_service.check_can_read(employee(2, "EMP002"), document_id) == "1/x.pdf"


def test_check_can_read_refuses_someone_with_no_access(store):
    document_id = store.seed_document(owner_id=1)
    with pytest.raises(AuthError) as error:
        documents_service.check_can_read(employee(2, "EMP002"), document_id)
    assert error.value.status_code == 403


def test_check_can_read_refuses_someone_whose_approval_expired(store):
    document_id = store.seed_document(owner_id=1)
    past = (get_current_time() - timedelta(days=1)).isoformat()
    store.seed_request(document_id, requester_id=2, status="approved", expires_at=past)
    with pytest.raises(AuthError) as error:
        documents_service.check_can_read(employee(2, "EMP002"), document_id)
    assert error.value.status_code == 403


def test_check_can_delete_allows_only_the_owner(store):
    document_id = store.seed_document(owner_id=1)
    document = documents_service.check_can_delete(employee(1, "EMP001"), document_id)
    assert document["id"] == document_id
    with pytest.raises(AuthError) as error:
        documents_service.check_can_delete(employee(2, "EMP002"), document_id)
    assert error.value.status_code == 403
