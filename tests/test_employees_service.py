"""Tests for app/hr/employees/service.py. A fake in-memory store replaces Supabase."""
from datetime import date

import pytest

from app.hr.employees import service as employees_service
from app.hr.employees import store as employee_store
from app.identity.auth import AuthError


class FakeStore:
    """Behaves like employee_store, but keeps everything in one dictionary."""

    def __init__(self):
        self.employees = {}
        self.next_id = 1

    def seed(self, emp_id="EMP001", email="a@x.com"):
        self.employees[self.next_id] = {"id": self.next_id, "emp_id": emp_id, "email": email, "full_name": "Someone"}
        self.next_id += 1

    def find_employee_with_login(self, emp_id=None, employee_id=None):
        for employee in self.employees.values():
            if employee["emp_id"] == emp_id or employee["id"] == employee_id:
                return dict(employee, login=None)
        return None

    def find_employee_by_email(self, email):
        for employee in self.employees.values():
            if employee["email"] == email:
                return dict(employee)
        return None

    def insert_employee(self, fields):
        row = dict(fields, id=self.next_id)
        self.employees[self.next_id] = row
        self.next_id += 1
        return row


@pytest.fixture
def store(monkeypatch):
    fake_store = FakeStore()
    monkeypatch.setattr(employee_store, "find_employee_with_login", fake_store.find_employee_with_login)
    monkeypatch.setattr(employee_store, "find_employee_by_email", fake_store.find_employee_by_email)
    monkeypatch.setattr(employee_store, "insert_employee", fake_store.insert_employee)
    return fake_store


def test_creates_the_employee_and_returns_a_summary(store):
    result = employees_service.create_employee({
        "emp_id": "EMP100", "full_name": "  Nina Rao  ", "email": "nina@x.com", "dept": "IT",
        "date_of_birth": date(1995, 3, 7), "date_of_joining": date(2026, 1, 1), "salary": 50000,
    })
    assert result == {"emp_id": "EMP100", "full_name": "Nina Rao", "email": "nina@x.com", "dept": "IT"}
    saved = next(iter(store.employees.values()))
    assert saved["dob"] == "1995-03-07"
    assert saved["doj"] == "2026-01-01"
    assert saved["is_confidential"] is False


def test_rejects_an_employee_id_already_used(store):
    store.seed(emp_id="EMP100", email="old@x.com")
    with pytest.raises(AuthError) as error:
        employees_service.create_employee({"emp_id": "EMP100", "full_name": "Someone Else", "email": "new@x.com"})
    assert error.value.status_code == 409
    assert "employee ID" in error.value.message


def test_rejects_an_email_already_used(store):
    store.seed(emp_id="EMP999", email="taken@x.com")
    with pytest.raises(AuthError) as error:
        employees_service.create_employee({"emp_id": "EMP100", "full_name": "Someone Else", "email": "taken@x.com"})
    assert error.value.status_code == 409
    assert "email" in error.value.message


def test_rejects_blank_required_fields(store):
    with pytest.raises(AuthError) as error:
        employees_service.create_employee({"emp_id": "  ", "full_name": "Someone", "email": "a@x.com"})
    assert error.value.status_code == 400


def test_optional_fields_default_to_none_when_missing(store):
    employees_service.create_employee({"emp_id": "EMP200", "full_name": "Plain Person", "email": "plain@x.com"})
    saved = next(iter(store.employees.values()))
    assert saved["dept"] is None
    assert saved["phone"] is None
    assert saved["dob"] is None
    assert saved["is_confidential"] is False
