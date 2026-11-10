"""Tests for the leave endpoints in app/main.py (website side). The database is FakeDatabase, storage is faked."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.identity import auth as auth
from app.hr.leave import service as leave_service
from app import main
from app.api.limits import MAXIMUM_DOCUMENT_BYTES
from fakes import FakeDatabase

client = TestClient(main.app)
HEADERS = {"Authorization": "Bearer any-token"}


def make_employee(employee_id=3, role="employee", must_change_password=False):
    return {"employee_id": employee_id, "emp_id": f"EMP00{employee_id}", "full_name": "Test", "dept": "IT",
            "role": role, "must_change_password": must_change_password}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(leave_service, "get_today", lambda: date(2026, 11, 7))
    database = FakeDatabase().install(monkeypatch)

    class Api:
        employee = make_employee()
        uploads = []
    holder = Api()
    holder.database = database
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: holder.employee)
    monkeypatch.setattr(main.storage_store, "upload_file", lambda bucket, path, data, content_type: holder.uploads.append((bucket, path, content_type, len(data))))
    monkeypatch.setattr(main.storage_store, "create_signed_link", lambda bucket, path: f"https://signed.example/{path}?token=abc")
    return holder


def test_every_leave_endpoint_needs_a_login_and_a_changed_password(api):
    assert client.get("/leave/balances").status_code == 401
    api.employee = make_employee(must_change_password=True)
    response = client.get("/leave/balances", headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["code"] == "must_change_password"


def test_me_works_even_with_a_temporary_password(api):
    api.employee = make_employee(must_change_password=True)
    body = client.get("/me", headers=HEADERS).json()
    assert body["emp_id"] == "EMP003" and body["must_change_password"] is True
    assert "employee_id" not in body                                    # internal ids stay inside


def test_balances_and_my_leaves(api):
    api.database.add_leave(3, "casual", "2026-06-01", "2026-06-04", 4, status="approved")
    balances = client.get("/leave/balances", headers=HEADERS).json()
    casual = next(row for row in balances if row["type_code"] == "casual")
    assert casual["used_days"] == 4 and casual["remaining_days"] == 4
    mine = client.get("/leave/mine", headers=HEADERS).json()
    assert mine[0]["description"].startswith("Casual Leave from Mon, 1 Jun 2026")
    assert client.get("/leave/mine?status=pending", headers=HEADERS).json() == []
    assert client.get("/leave/mine?status=bogus", headers=HEADERS).status_code == 422


def test_apply_on_the_website(api):
    response = client.post("/leave/apply", json={"leave_type": "casual", "start_date": "2026-11-10"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["approver"] == "Rahul Verma"
    assert api.database.leave_requests[0]["status"] == "pending"


def test_a_rule_error_is_a_400_with_the_message_and_a_code(api):
    response = client.post("/leave/apply", json={"leave_type": "casual", "start_date": "2026-10-22"}, headers=HEADERS)
    assert response.status_code == 400
    assert "7 days back" in response.json()["detail"]
    assert response.json()["code"] == "leave_rule"


def test_bad_input_is_a_422(api):
    assert client.post("/leave/apply", json={"leave_type": "casual", "start_date": "not-a-date"}, headers=HEADERS).status_code == 422
    assert client.post("/leave/apply", json={"start_date": "2026-11-10"}, headers=HEADERS).status_code == 422


def test_maternity_needs_a_document_uploaded_on_the_website(api):
    body = {"leave_type": "maternity", "start_date": "2026-11-10", "end_date": "2027-02-07"}
    assert client.post("/leave/apply", json=body, headers=HEADERS).status_code == 400
    upload = client.post("/leave/documents?filename=doctor note.pdf", content=b"%PDF-1.4 test",
                         headers={**HEADERS, "Content-Type": "application/pdf"})
    assert upload.status_code == 200
    document_path = upload.json()["document_path"]
    assert document_path.startswith("3/") and "doctor_note.pdf" in document_path       # safe file name
    response = client.post("/leave/apply", json=dict(body, document_path=document_path), headers=HEADERS)
    assert response.status_code == 200 and response.json()["approver"] == "HR"


def test_upload_limits(api):
    pdf = {**HEADERS, "Content-Type": "application/pdf"}
    assert client.post("/leave/documents?filename=a.exe", content=b"x", headers={**HEADERS, "Content-Type": "application/x-msdownload"}).status_code == 415
    assert client.post("/leave/documents?filename=a.pdf", content=b"", headers=pdf).status_code == 400
    too_big = b"x" * (MAXIMUM_DOCUMENT_BYTES + 1)
    assert client.post("/leave/documents?filename=a.pdf", content=too_big, headers=pdf).status_code == 413
    assert api.uploads == []
    assert client.post("/leave/documents", content=b"x", headers=pdf).status_code == 422          # filename is required


def test_a_failed_upload_is_a_502_without_internal_details(api, monkeypatch):
    def broken(bucket, path, data, content_type):
        raise RuntimeError("secret key sb_secret_1")
    monkeypatch.setattr(main.storage_store, "upload_file", broken)
    response = client.post("/leave/documents?filename=a.pdf", content=b"x", headers={**HEADERS, "Content-Type": "application/pdf"})
    assert response.status_code == 502 and "secret" not in response.text


def test_a_document_path_of_someone_else_is_refused(api):
    body = {"leave_type": "sick", "start_date": "2026-11-10", "document_path": "4/their-note.pdf"}
    assert client.post("/leave/apply", json=body, headers=HEADERS).status_code == 403


def test_cancel_my_leave(api):
    leave = api.database.add_leave(3, "casual", "2026-11-20", "2026-11-20", 1, status="pending")
    assert client.post(f"/leave/{leave['id']}/cancel", headers=HEADERS).status_code == 200
    assert client.post(f"/leave/{leave['id']}/cancel", headers=HEADERS).status_code == 400        # already cancelled
    api.employee = make_employee(4)
    other = api.database.add_leave(3, "casual", "2026-11-23", "2026-11-23", 1, status="pending")
    assert client.post(f"/leave/{other['id']}/cancel", headers=HEADERS).status_code == 404       # not theirs


def test_the_manager_sees_and_decides_the_teams_leaves(api):
    leave = api.database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    api.employee = make_employee(2)                                                               # Rahul, the manager
    approvals = client.get("/leave/approvals", headers=HEADERS).json()
    assert [item["id"] for item in approvals] == [leave["id"]]
    assert approvals[0]["full_name"] == "Ananya Das"
    response = client.post(f"/leave/{leave['id']}/decision", json={"decision": "approved"}, headers=HEADERS)
    assert response.json()["status"] == "approved"


def test_a_colleague_cannot_decide_and_the_employee_cannot_approve_themselves(api):
    leave = api.database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    api.employee = make_employee(4)
    assert client.post(f"/leave/{leave['id']}/decision", json={"decision": "approved"}, headers=HEADERS).status_code == 403
    assert client.get("/leave/approvals", headers=HEADERS).json() == []
    api.employee = make_employee(3)
    assert client.post(f"/leave/{leave['id']}/decision", json={"decision": "approved"}, headers=HEADERS).status_code == 403
    assert api.database.leave_requests[0]["status"] == "pending"


def test_a_rejection_without_a_reason_is_refused_over_the_api(api):
    api.employee = make_employee(1, role="hr")
    leave = api.database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    response = client.post(f"/leave/{leave['id']}/decision", json={"decision": "rejected"}, headers=HEADERS)
    assert response.status_code == 400 and "reason for rejecting" in response.json()["detail"]
    assert api.database.leave_requests[0]["status"] == "pending"


def test_my_leaves_show_who_decided_each_one(api):
    api.database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="rejected")
    api.database.leave_requests[0].update(approver_id=2, decision_note="team is short that week")
    rows = client.get("/leave/mine", headers=HEADERS).json()
    assert rows[0]["decided_by_name"] == "Rahul Verma"          # employee 2 is the one who decided
    assert rows[0]["decision_note"] == "team is short that week"


def test_decision_values_are_checked(api):
    api.employee = make_employee(1, role="hr")
    leave = api.database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    assert client.post(f"/leave/{leave['id']}/decision", json={"decision": "maybe"}, headers=HEADERS).status_code == 422
    assert client.post("/leave/999/decision", json={"decision": "approved"}, headers=HEADERS).status_code == 404


def test_document_links_follow_the_same_visibility_rules(api):
    leave = api.database.add_leave(3, "sick", "2026-11-10", "2026-11-10", 1, status="pending", document_path="3/note.pdf")
    api.employee = make_employee(2)
    assert client.get(f"/leave/{leave['id']}/document-url", headers=HEADERS).json()["url"].startswith("https://signed.example/3/note.pdf")
    api.employee = make_employee(4)
    assert client.get(f"/leave/{leave['id']}/document-url", headers=HEADERS).status_code == 403


def test_attach_a_document_to_the_waiting_leave(api):
    action = api.database.create_pending_action(3, "apply_leave", {"leave_type": "sick"}, "Sick Leave")
    response = client.post(f"/pending-actions/{action['id']}/document", json={"document_path": "3/note.pdf"}, headers=HEADERS)
    assert response.status_code == 200
    assert action["payload"]["document_path"] == "3/note.pdf"


def test_too_many_attempts_sends_retry_after_and_a_code(monkeypatch):
    def locked(emp_id, password):
        raise auth.AuthError(429, "Too many failed attempts. Please try again in a few minutes.")
    monkeypatch.setattr(main.auth, "authenticate", locked)
    response = client.post("/login", json={"emp_id": "EMP001", "password": "x"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(auth.LOCK_MINUTES * 60)
    assert response.json()["code"] == "too_many_attempts"


def test_login_failures_carry_a_code(monkeypatch):
    def failed(emp_id, password):
        raise auth.AuthError(401, auth.LOGIN_FAILED_MESSAGE, "invalid_credentials")
    monkeypatch.setattr(main.auth, "authenticate", failed)
    assert client.post("/login", json={"emp_id": "EMP001", "password": "x"}).json()["code"] == "invalid_credentials"
