"""Tests for the WhatsApp HR report (app/hr/reports/*): the number list, the report text, and sending."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import main
from app.hr.reports import aisensy
from app.hr.reports import service as reports_service
from app.hr.reports import store as reports_store
from app.hr.reports.service import ReportError
from fakes_attendance import FakeAttendanceDatabase, login_of

SUPERADMIN = login_of(8)
HR = login_of(1)
EMPLOYEE = login_of(3)
LOCAL_IP = "203.0.113.7"


@pytest.fixture
def database(monkeypatch):
    return FakeAttendanceDatabase().install(monkeypatch)


@pytest.fixture
def numbers_db(monkeypatch):
    """An in-memory stand-in for the whatsapp_report_numbers table."""
    rows = []
    next_id = {"value": 0}

    def list_numbers():
        return list(rows)

    def insert_number(label, phone_number):
        if any(row["phone_number"] == phone_number for row in rows):
            return None
        next_id["value"] += 1
        row = {"id": next_id["value"], "label": label, "phone_number": phone_number, "created_at": "2026-11-16T00:00:00Z"}
        rows.append(row)
        return row

    def delete_number(number_id):
        for index, row in enumerate(rows):
            if row["id"] == number_id:
                return rows.pop(index)
        return None

    monkeypatch.setattr(reports_store, "list_numbers", list_numbers)
    monkeypatch.setattr(reports_store, "insert_number", insert_number)
    monkeypatch.setattr(reports_store, "delete_number", delete_number)
    return rows


# ------------------------------------------------------------------ the number list (super admin only)
def test_only_the_super_admin_manages_the_numbers(numbers_db):
    with pytest.raises(ReportError, match="Only the super admin"):
        reports_service.list_numbers(HR)
    with pytest.raises(ReportError, match="Only the super admin"):
        reports_service.add_number(HR, "Debayan", "+919876543210")
    with pytest.raises(ReportError, match="Only the super admin"):
        reports_service.remove_number(HR, 1)


def test_adding_and_removing_a_number(numbers_db):
    saved = reports_service.add_number(SUPERADMIN, "Debayan", "+91 98765 43210")
    assert saved["label"] == "Debayan" and saved["phone_number"] == "+919876543210"
    assert reports_service.list_numbers(SUPERADMIN) == [saved]

    removed = reports_service.remove_number(SUPERADMIN, saved["id"])
    assert removed["id"] == saved["id"]
    assert reports_service.list_numbers(SUPERADMIN) == []

    with pytest.raises(ReportError, match="could not find"):
        reports_service.remove_number(SUPERADMIN, saved["id"])


def test_a_number_needs_a_label(numbers_db):
    with pytest.raises(ReportError, match="label"):
        reports_service.add_number(SUPERADMIN, "   ", "+919876543210")


def test_an_invalid_number_is_refused(numbers_db):
    for bad in ("12345", "not a number", "++91", "123456789012345678"):
        with pytest.raises(ReportError, match="does not look like"):
            reports_service.add_number(SUPERADMIN, "Someone", bad)


def test_the_same_number_cannot_be_added_twice(numbers_db):
    reports_service.add_number(SUPERADMIN, "Debayan", "+919876543210")
    with pytest.raises(ReportError, match="already on the list"):
        reports_service.add_number(SUPERADMIN, "Debayan again", "+919876543210")


def test_at_most_ten_numbers(numbers_db):
    for index in range(reports_service.MAXIMUM_NUMBERS):
        reports_service.add_number(SUPERADMIN, f"Number {index}", f"+9198765432{index:02d}")
    with pytest.raises(ReportError, match="already 10 numbers"):
        reports_service.add_number(SUPERADMIN, "One too many", "+919999999999")


# ------------------------------------------------------------------ the report text
def test_build_daily_report_text_groups_people_by_status(database):
    # Ann Employee (3): present, on time. Hana HR (1): present, but late. Sal Sales (4): nothing
    # saved and not on leave, so "not checked in". Harry HR (7): on casual leave. Pam PM (2): WFH.
    # Cleo CEO (5), Tia CTO (6, confidential) and Sue Super (8, the super admin) have no attendance
    # at all (no check-in feature exists for them anywhere in the app), so none of them should
    # appear anywhere in the report, including the check-in-times section.
    database.set_time("10:05", "2026-11-12")
    main_service = reports_service.attendance_service
    main_service.check_in(EMPLOYEE, LOCAL_IP)
    database.set_time("19:00", "2026-11-12")
    main_service.check_out(EMPLOYEE, LOCAL_IP)

    database.set_time("10:50", "2026-11-12")
    main_service.check_in(HR, LOCAL_IP)
    database.set_time("19:00", "2026-11-12")
    main_service.check_out(HR, LOCAL_IP)

    database.leaves.append({"employee_id": 7, "leave_type": "casual", "start_date": "2026-11-12", "end_date": "2026-11-12", "day_type": "full"})
    database.leaves.append({"employee_id": 2, "leave_type": "wfh", "start_date": "2026-11-12", "end_date": "2026-11-12", "day_type": "full"})

    text = reports_service.build_daily_report_text(date(2026, 11, 12))
    assert "Daily HR Report - Thu, 12 Nov 2026" in text
    assert "*Attendance (5 tracked)*" in text        # Hana, Pam, Ann, Sal, Harry - not the CEO/CTO/super admin
    assert "Present: 2" in text
    assert "Late: Hana HR" in text
    assert "Not checked in / absent: Sal Sales" in text
    assert "*On leave today (1)*" in text
    assert "- Harry HR - Casual Leave (full day)" in text
    assert "*Work From Home today (1)*" in text
    assert "- Pam PM" in text
    assert "*Check-in times (2)*" in text
    assert "- Ann Employee - 10:05" in text
    assert "- Hana HR - 10:50" in text
    assert "This is an automated report" not in text     # that's the AiSensy template's static Footer, not generated here


def test_build_daily_report_text_with_nobody_on_leave_or_wfh(database):
    text = reports_service.build_daily_report_text(date(2026, 11, 12))
    assert "*On leave today (0)*" in text and "- Nobody" in text
    assert "*Work From Home today (0)*" in text
    assert "*Check-in times (0)*" in text and "- Nobody has checked in yet" in text


def test_the_report_notes_a_weekend_or_a_company_holiday(database):
    assert "Today is a weekend." in reports_service.build_daily_report_text(date(2026, 11, 14))     # a Saturday
    assert "Today is a company holiday (Christmas)." in reports_service.build_daily_report_text(date(2026, 12, 25))


# ------------------------------------------------------------------ the report fields (daily_hr_report_v2 - 8 separate AiSensy template variables)
def test_build_daily_report_fields_groups_people_by_status(database):
    # Same scenario as test_build_daily_report_text_groups_people_by_status above.
    database.set_time("10:05", "2026-11-12")
    main_service = reports_service.attendance_service
    main_service.check_in(EMPLOYEE, LOCAL_IP)
    database.set_time("19:00", "2026-11-12")
    main_service.check_out(EMPLOYEE, LOCAL_IP)

    database.set_time("10:50", "2026-11-12")
    main_service.check_in(HR, LOCAL_IP)
    database.set_time("19:00", "2026-11-12")
    main_service.check_out(HR, LOCAL_IP)

    database.leaves.append({"employee_id": 7, "leave_type": "casual", "start_date": "2026-11-12", "end_date": "2026-11-12", "day_type": "full"})
    database.leaves.append({"employee_id": 2, "leave_type": "wfh", "start_date": "2026-11-12", "end_date": "2026-11-12", "day_type": "full"})

    fields = reports_service.build_daily_report_fields(date(2026, 11, 12))
    assert fields == [
        "Thu, 12 Nov 2026",
        "5",                      # Hana, Pam, Ann, Sal, Harry tracked - not the CEO/CTO/super admin
        "2",                      # present: Ann, Hana
        "Hana HR",                # late
        "Sal Sales",              # not checked in / absent
        "Harry HR (Casual Leave, full day)",
        "Pam PM",
        "Hana HR 10:50, Ann Employee 10:05",
    ]


def test_build_daily_report_fields_with_nobody_on_leave_or_wfh(database):
    # Nothing recorded for anyone today, so everyone tracked defaults to "not checked in" (absent)
    # - that is what fields[4] covers. Nobody is late (no check-in to be late against), nobody is
    # on leave or WFH, and nobody has a check-in time yet - that is what this test actually checks.
    fields = reports_service.build_daily_report_fields(date(2026, 11, 12))
    assert fields[3] == "none"                        # late
    assert fields[5] == "Nobody"                       # on leave
    assert fields[6] == "Nobody"                       # WFH
    assert fields[7] == "Nobody has checked in yet"    # check-in times


# ------------------------------------------------------------------ AiSensy
def test_sending_without_aisensy_configured_is_refused(monkeypatch):
    monkeypatch.delenv("AISENSY_API_KEY", raising=False)
    monkeypatch.delenv("AISENSY_CAMPAIGN_NAME", raising=False)
    assert aisensy.is_configured() is False
    with pytest.raises(aisensy.AiSensyError, match="not set up yet"):
        aisensy.send_report("+919876543210", ["hello"])


def test_sending_posts_the_expected_payload(monkeypatch):
    monkeypatch.setenv("AISENSY_API_KEY", "key123")
    monkeypatch.setenv("AISENSY_CAMPAIGN_NAME", "daily_report")
    captured = {}

    class FakeResponse:
        ok = True

        def json(self):
            return {"success": True}

    def fake_post(url, json, timeout):
        captured.update(url=url, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(aisensy.requests, "post", fake_post)
    result = aisensy.send_report("+919876543210", ["report text"])
    assert result == {"success": True}
    assert captured["json"] == {"apiKey": "key123", "campaignName": "daily_report", "destination": "919876543210",
                               "userName": aisensy.DEFAULT_SENDER_NAME, "templateParams": ["report text"]}


def test_sending_raises_on_a_bad_response(monkeypatch):
    monkeypatch.setenv("AISENSY_API_KEY", "key123")
    monkeypatch.setenv("AISENSY_CAMPAIGN_NAME", "daily_report")

    class FakeResponse:
        ok = False
        status_code = 400
        text = "bad template"

    monkeypatch.setattr(aisensy.requests, "post", lambda *args, **kwargs: FakeResponse())
    with pytest.raises(aisensy.AiSensyError, match="refused"):
        aisensy.send_report("+919876543210", ["report text"])


def test_send_daily_report_tries_every_number_even_if_one_fails(monkeypatch, numbers_db):
    numbers_db.append({"id": 1, "label": "A", "phone_number": "+911111111111", "created_at": "x"})
    numbers_db.append({"id": 2, "label": "B", "phone_number": "+922222222222", "created_at": "x"})
    monkeypatch.setattr(reports_service, "build_daily_report_fields", lambda for_date=None: ["REPORT"] * 8)

    attempted = []

    def fake_send(number, fields):
        attempted.append(number)
        if number == "+911111111111":
            raise aisensy.AiSensyError("boom")
        return {"ok": True}

    monkeypatch.setattr(aisensy, "send_report", fake_send)

    results = reports_service.send_daily_report()
    assert attempted == ["+911111111111", "+922222222222"]           # the second number is still tried
    assert results[0] == {"label": "A", "phone_number": "+911111111111", "ok": False, "error": "boom"}
    assert results[1] == {"label": "B", "phone_number": "+922222222222", "ok": True, "error": None}


def test_send_daily_report_with_no_numbers_configured(monkeypatch, numbers_db):
    monkeypatch.setattr(reports_service, "build_daily_report_fields", lambda for_date=None: ["REPORT"] * 8)
    assert reports_service.send_daily_report() == []


# ------------------------------------------------------------------ the HTTP endpoints
client = TestClient(main.app)
HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
def api(monkeypatch, numbers_db):
    class Holder:
        employee = dict(SUPERADMIN, must_change_password=False)
    holder = Holder()
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: holder.employee)
    return holder


def test_endpoints_list_add_and_remove_a_number(api):
    assert client.get("/superadmin/whatsapp-numbers", headers=HEADERS).json() == []

    response = client.post("/superadmin/whatsapp-numbers", json={"label": "Debayan", "phone_number": "+919876543210"}, headers=HEADERS)
    assert response.status_code == 200
    saved = response.json()
    assert saved["label"] == "Debayan"

    assert len(client.get("/superadmin/whatsapp-numbers", headers=HEADERS).json()) == 1

    delete_response = client.delete(f"/superadmin/whatsapp-numbers/{saved['id']}", headers=HEADERS)
    assert delete_response.status_code == 200
    assert client.get("/superadmin/whatsapp-numbers", headers=HEADERS).json() == []
    assert client.delete(f"/superadmin/whatsapp-numbers/{saved['id']}", headers=HEADERS).status_code == 404


def test_only_the_super_admin_may_use_the_endpoints(api):
    api.employee = dict(HR, must_change_password=False)
    assert client.get("/superadmin/whatsapp-numbers", headers=HEADERS).status_code == 403
    assert client.post("/superadmin/whatsapp-numbers", json={"label": "X", "phone_number": "+919876543210"}, headers=HEADERS).status_code == 403
    assert client.delete("/superadmin/whatsapp-numbers/1", headers=HEADERS).status_code == 403
    assert client.post("/superadmin/whatsapp-report/send-now", headers=HEADERS).status_code == 403


def test_send_now_endpoint_uses_the_service(api, monkeypatch):
    monkeypatch.setattr(main.reports_service, "send_daily_report", lambda for_date=None: [{"label": "Debayan", "phone_number": "+919876543210", "ok": True, "error": None}])
    response = client.post("/superadmin/whatsapp-report/send-now", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == [{"label": "Debayan", "phone_number": "+919876543210", "ok": True, "error": None}]
