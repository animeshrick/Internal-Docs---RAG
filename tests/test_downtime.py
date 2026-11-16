"""Tests for app/core/downtime.py and the middleware/endpoint in app/main.py that use it."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import downtime
from app.hr.reports import store as reports_store


# ------------------------------------------------------------------ page_key_for_path
@pytest.mark.parametrize("path,expected", [
    ("/payroll/payslips/mine/years", "payslips"),
    ("/payroll/payslips/mine", "payslips"),
    ("/leave/apply", "leave_and_wfh"),
    ("/leave/mine", "leave_and_wfh"),
    ("/pending-actions/7/document", "leave_and_wfh"),
    ("/attendance/today", "attendance"),
    ("/attendance/team", "attendance"),
    ("/celebrations", "home"),
    ("/documents", "documents"),
    ("/documents/5/download-url", "documents"),
    ("/admin/employees", "admin"),
    ("/register", "admin"),
    ("/reset-password", "admin"),
    ("/superadmin/whatsapp-numbers", "superadmin"),
    ("/chat", "chat"),
    ("/chat/history", "chat"),
    ("/me", "me"),
    ("/me/profile", "me"),
])
def test_page_key_for_path_matches_the_right_group(path, expected):
    assert downtime.page_key_for_path(path) == expected


@pytest.mark.parametrize("path", ["/health", "/login", "/change-password", "/system/downtime"])
def test_page_key_for_path_never_matches_the_always_on_routes(path):
    assert downtime.page_key_for_path(path) is None


# ------------------------------------------------------------------ get_down_pages
def test_get_down_pages_with_nothing_set(monkeypatch):
    monkeypatch.delenv("DOWNTIME_PAGES", raising=False)
    assert downtime.get_down_pages() == set()


def test_get_down_pages_parses_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("DOWNTIME_PAGES", "payslips, admin ,superadmin")
    assert downtime.get_down_pages() == {"payslips", "admin", "superadmin"}


def test_get_down_pages_ignores_an_unknown_key(monkeypatch):
    monkeypatch.setenv("DOWNTIME_PAGES", "payslips,not_a_real_page")
    assert downtime.get_down_pages() == {"payslips"}


# ------------------------------------------------------------------ the endpoint and the middleware
client = TestClient(main.app)
HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
def api(monkeypatch):
    class Holder:
        employee = {"employee_id": 8, "emp_id": "EMP008", "full_name": "Sue Super", "dept": "Management", "role": "superadmin", "must_change_password": False}
    holder = Holder()
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: holder.employee)
    return holder


def test_system_downtime_endpoint_needs_no_login(monkeypatch):
    monkeypatch.setenv("DOWNTIME_PAGES", "payslips,admin")
    response = client.get("/system/downtime")
    assert response.status_code == 200
    assert response.json() == {"down_pages": ["admin", "payslips"]}


def test_a_page_marked_down_refuses_its_routes_with_503(api, monkeypatch):
    monkeypatch.setenv("DOWNTIME_PAGES", "payslips")
    response = client.get("/payroll/payslips/mine/years", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": downtime.DOWNTIME_MESSAGE, "code": "page_down", "page": "payslips"}


def test_a_different_page_is_unaffected(api, monkeypatch):
    monkeypatch.setattr(reports_store, "list_numbers", lambda: [])
    monkeypatch.setenv("DOWNTIME_PAGES", "payslips")
    response = client.get("/superadmin/whatsapp-numbers", headers=HEADERS)
    assert response.status_code == 200


def test_health_is_never_blocked(monkeypatch):
    monkeypatch.setenv("DOWNTIME_PAGES", "home,chat,documents,leave_and_wfh,payslips,approvals,attendance,me,admin,superadmin")
    assert client.get("/health").status_code == 200


def test_nothing_is_blocked_once_the_setting_is_cleared(api, monkeypatch):
    monkeypatch.setattr(main.payroll_store, "get_salary_details", lambda employee_id: None)
    monkeypatch.delenv("DOWNTIME_PAGES", raising=False)
    response = client.get("/payroll/payslips/mine/years", headers=HEADERS)
    assert response.status_code == 200
