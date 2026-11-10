"""Tests for app/employee_store.py. requests.get/post/patch are replaced with fakes."""
import pytest
import requests

import employee_store


class FakeResponse:
    def __init__(self, body=None, status_code=200):
        self._body = body if body is not None else []
        self.status_code = status_code
        self.text = str(self._body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def supabase_settings(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")


def test_find_by_emp_id_builds_a_safe_filter_and_flattens_the_login(monkeypatch):
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append((url, params))
        return FakeResponse([{"id": 5, "emp_id": "EMP005", "employee_auth": {"role": "hr"}}])

    monkeypatch.setattr(requests, "get", fake_get)
    employee = employee_store.find_employee_with_login(emp_id="EMP005")

    assert calls[0][0] == "https://example.supabase.co/rest/v1/employee"
    assert calls[0][1]["emp_id"] == "eq.EMP005"
    assert employee["login"] == {"role": "hr"}
    assert "employee_auth" not in employee


def test_find_by_database_id_uses_the_id_column(monkeypatch):
    seen = {}
    monkeypatch.setattr(requests, "get", lambda url, headers, params, timeout: seen.update(params) or FakeResponse([]))
    assert employee_store.find_employee_with_login(employee_id=7) is None
    assert seen["id"] == "eq.7"


def test_login_can_come_back_as_a_list_or_as_none(monkeypatch):
    rows = [{"id": 1, "employee_auth": [{"role": "ceo"}]}]
    monkeypatch.setattr(requests, "get", lambda url, headers, params, timeout: FakeResponse(rows))
    assert employee_store.find_employee_with_login(emp_id="A")["login"] == {"role": "ceo"}

    rows = [{"id": 1, "employee_auth": []}]
    monkeypatch.setattr(requests, "get", lambda url, headers, params, timeout: FakeResponse(rows))
    assert employee_store.find_employee_with_login(emp_id="A")["login"] is None


def test_find_needs_exactly_one_identifier():
    with pytest.raises(ValueError):
        employee_store.find_employee_with_login()
    with pytest.raises(ValueError):
        employee_store.find_employee_with_login(emp_id="A", employee_id=1)


def test_database_errors_are_reported(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, headers, params, timeout: FakeResponse([], status_code=500))
    with pytest.raises(RuntimeError):
        employee_store.find_employee_with_login(emp_id="A")


def test_insert_and_update_send_the_right_requests(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent["post"] = (url, json)
        return FakeResponse()

    def fake_patch(url, headers, params, json, timeout):
        sent["patch"] = (url, params, json)
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "patch", fake_patch)

    employee_store.insert_login(5, "hash", "hr")
    employee_store.update_login(5, {"failed_login_attempts": 2})

    assert sent["post"] == ("https://example.supabase.co/rest/v1/employee_auth",
                            {"employee_id": 5, "password_hash": "hash", "role": "hr"})
    assert sent["patch"][1] == {"employee_id": "eq.5"}
    assert sent["patch"][2] == {"failed_login_attempts": 2}


def test_list_employees_without_login_skips_inactive_and_existing(monkeypatch):
    employees = [
        {"id": 1, "emp_id": "A", "full_name": "A", "is_active": True},
        {"id": 2, "emp_id": "B", "full_name": "B", "is_active": True},
        {"id": 3, "emp_id": "C", "full_name": "C", "is_active": False},
    ]

    def fake_get(url, headers, params, timeout):
        return FakeResponse(employees if url.endswith("/employee") else [{"employee_id": 1}])

    monkeypatch.setattr(requests, "get", fake_get)
    assert [row["emp_id"] for row in employee_store.list_employees_without_login()] == ["B"]
