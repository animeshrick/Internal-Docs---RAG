"""Tests for the own-profile endpoint (GET /me/profile)."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.hr.people import store as people_store

client = TestClient(main.app)
HEADERS = {"Authorization": "Bearer any-token"}
EMPLOYEE = {"employee_id": 4, "emp_id": "EMP-1004", "full_name": "Taylor Bennett", "dept": "IT", "role": "employee", "must_change_password": False}
ROW = {"id": 4, "emp_id": "EMP-1004", "full_name": "Taylor Bennett", "email": "t@example.com", "phone": "+91 1", "dept": "IT",
       "dob": "1985-03-09", "doj": "2022-01-03", "gender": "Male", "manager_id": 2, "is_active": True, "is_confidential": False,
       "full_current_address": None, "emergency_ph_no": None, "salary": 99999, "role": "employee"}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: EMPLOYEE)
    asked = {}
    monkeypatch.setattr(people_store, "get_profile", lambda employee_id: asked.update(id=employee_id) or dict(ROW))
    monkeypatch.setattr(main.people_service.leave_store, "get_employee_row", lambda employee_id: {"full_name": "Rahul Verma"})
    return asked


def test_my_profile_shows_my_details_and_my_manager(fakes):
    response = client.get("/me/profile", headers=HEADERS)
    body = response.json()
    assert response.status_code == 200 and fakes["id"] == 4
    assert (body["full_name"], body["dept"], body["manager_name"], body["date_of_joining"]) == ("Taylor Bennett", "IT", "Rahul Verma", "2022-01-03")
    assert body["address"] is None


def test_my_profile_never_includes_the_salary_or_the_confidential_flag():
    body = client.get("/me/profile", headers=HEADERS).json()
    assert "salary" not in body and "is_confidential" not in body


def test_my_profile_needs_a_login():
    assert client.get("/me/profile").status_code == 401
