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
    ("/messages/conversations", "messages"),
    ("/messages/thread/EMP003", "messages"),
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
        employee = {"employee_id": 8, "emp_id": "EMP008", "full_name": "Sue Super", "dept": "Management", "role": "superadmin", "must_change_password": False, "has_government_id": True}
    holder = Holder()
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: holder.employee)
    return holder


def test_system_downtime_endpoint_needs_no_login(monkeypatch, tmp_path):
    monkeypatch.setenv("DOWNTIME_PAGES", "payslips,admin")
    monkeypatch.setattr(downtime, "PROJECT_ROOT", tmp_path)      # ignore this machine's own .env
    monkeypatch.delenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, raising=False)
    response = client.get("/system/downtime")
    assert response.status_code == 200
    assert response.json() == {"down_pages": ["admin", "payslips"], "emergency_broadcast": None}


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
    monkeypatch.setenv("DOWNTIME_PAGES", "home,chat,documents,leave_and_wfh,payslips,approvals,attendance,me,admin,superadmin,messages")
    assert client.get("/health").status_code == 200


def test_nothing_is_blocked_once_the_setting_is_cleared(api, monkeypatch):
    monkeypatch.setattr(main.payroll_store, "get_salary_details", lambda employee_id: None)
    monkeypatch.delenv("DOWNTIME_PAGES", raising=False)
    response = client.get("/payroll/payslips/mine/years", headers=HEADERS)
    assert response.status_code == 200


# ------------------------------------------------------------------ the emergency broadcast
# A short notice shown at the top of every page of the website. It rides along with the downtime
# status because the frontend already polls that once a minute and it needs no login, so the
# notice reaches the login page too. Unset or empty means no banner.
@pytest.fixture
def no_env_file(monkeypatch):
    """Ignore the real .env on this machine, so these tests read only what they set themselves."""
    monkeypatch.setattr(downtime, "read_broadcast_from_env_file", lambda: None)


def test_no_broadcast_when_the_setting_is_not_set(monkeypatch, no_env_file):
    monkeypatch.delenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, raising=False)
    assert downtime.get_emergency_broadcast() is None


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_no_broadcast_for_an_empty_or_blank_setting(monkeypatch, no_env_file, value):
    monkeypatch.setenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, value)
    assert downtime.get_emergency_broadcast() is None


def test_the_message_is_trimmed_and_returned(monkeypatch, no_env_file):
    monkeypatch.setenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, "  Testing  ")
    assert downtime.get_emergency_broadcast() == "Testing"


def test_a_very_long_message_is_cut_off(monkeypatch, no_env_file):
    monkeypatch.setenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, "x" * 500)
    assert len(downtime.get_emergency_broadcast()) == downtime.MAXIMUM_BROADCAST_LENGTH


def test_the_public_endpoint_carries_the_broadcast(monkeypatch, no_env_file):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, "Testing")
    monkeypatch.setattr(main.downtime, "get_down_pages", lambda: set())
    body = TestClient(main.app).get("/system/downtime").json()
    assert body == {"down_pages": [], "emergency_broadcast": "Testing"}


def test_the_public_endpoint_says_null_when_there_is_no_broadcast(monkeypatch, no_env_file):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.delenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, raising=False)
    monkeypatch.setattr(main.downtime, "get_down_pages", lambda: set())
    assert TestClient(main.app).get("/system/downtime").json()["emergency_broadcast"] is None


# The notice has to be changeable on a server that is already running, so this one setting is
# read from the .env file on every call rather than once at startup.
def test_the_env_file_is_read_again_on_every_call(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(downtime, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, raising=False)

    env_file.write_text("EMERGENCY_BROADCAST=First notice\n", encoding="utf-8")
    assert downtime.get_emergency_broadcast() == "First notice"

    # Edited while the server keeps running: no restart, the next call sees the new text.
    env_file.write_text("EMERGENCY_BROADCAST=Second notice\n", encoding="utf-8")
    assert downtime.get_emergency_broadcast() == "Second notice"

    # Emptied: the notice comes down, again with no restart.
    env_file.write_text("EMERGENCY_BROADCAST=\n", encoding="utf-8")
    assert downtime.get_emergency_broadcast() is None


def test_the_env_file_wins_over_the_process_environment(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("EMERGENCY_BROADCAST=From the file\n", encoding="utf-8")
    monkeypatch.setattr(downtime, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, "From the environment")
    assert downtime.get_emergency_broadcast() == "From the file"


def test_the_process_environment_is_used_when_there_is_no_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(downtime, "PROJECT_ROOT", tmp_path)      # empty folder: no .env in it
    monkeypatch.setenv(downtime.EMERGENCY_BROADCAST_SETTING_NAME, "From the environment")
    assert downtime.get_emergency_broadcast() == "From the environment"
