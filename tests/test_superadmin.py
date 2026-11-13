"""Tests for what the super admin can do (app/hr/superadmin/service.py)."""
from datetime import date

import pytest

from app.hr.attendance import service as attendance_service
from app.hr.attendance.service import AttendanceError
from app.hr.leave import service as leave_service
from app.hr.leave import store as leave_store
from app.hr.leave.service import LeaveError
from app.hr.superadmin import service
from app.hr.superadmin import store as superadmin_store
from fakes_attendance import FakeAttendanceDatabase, login_of, row_of

HR, EMPLOYEE, CEO, ADMIN = (login_of(person_id) for person_id in (1, 3, 5, 8))

CASUAL = {"code": "casual", "name": "Casual Leave", "applies_to_gender": None, "can_apply_by_chat": True, "max_days_per_request": None,
          "limit_type": "per_year", "days_allowed": 12, "count_mode": "working_days"}


@pytest.fixture
def database(monkeypatch):
    fake = FakeAttendanceDatabase().install(monkeypatch)
    monkeypatch.setattr(leave_service, "get_now", lambda: fake.now)
    monkeypatch.setattr(leave_service, "get_today", lambda: date(2026, 11, 12))
    saved = []
    fake.saved_leaves = saved
    monkeypatch.setattr(leave_store, "get_employee_row", lambda employee_id: row_of(employee_id))
    monkeypatch.setattr(leave_store, "get_leave_types", lambda: {"casual": CASUAL})
    # Count Monday to Friday only (the real store asks the database, which also skips holidays).
    def count_days(start, end, day_type, mode):
        days = sum(1 for offset in range((end - start).days + 1) if date.fromordinal(start.toordinal() + offset).weekday() < 5)
        return days * (0.5 if day_type == "half" else 1)
    monkeypatch.setattr(leave_store, "count_leave_days", count_days)
    monkeypatch.setattr(leave_store, "find_overlapping_requests", lambda *args: [])
    monkeypatch.setattr(leave_store, "get_leave_balances", lambda employee_id, on_date: [{"type_code": "casual", "remaining_days": 5}])
    def insert(fields):
        saved.append(fields)
        return dict(fields, id=len(saved))
    monkeypatch.setattr(leave_store, "insert_leave_request", insert)

    audit_entries = []
    fake.audit_entries = audit_entries

    def insert_audit(fields):
        row = dict(fields, id=len(audit_entries) + 1, created_at=fake.now.isoformat())
        audit_entries.append(row)
        return row

    monkeypatch.setattr(superadmin_store, "insert_audit_entry", insert_audit)
    monkeypatch.setattr(superadmin_store, "list_audit_entries", lambda limit=50: list(reversed(audit_entries))[:limit])
    return fake


def test_only_the_super_admin_may_use_it(database):
    for person in (HR, EMPLOYEE, CEO):
        with pytest.raises(LeaveError) as error:
            service.list_employees(person)
        assert error.value.status_code == 403


def test_leave_is_approved_at_once_even_for_an_old_date(database):
    result = service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 8, 3), date(2026, 8, 4), "full", "forgot to apply")
    row = database.saved_leaves[0]
    assert (row["status"], row["approver_id"], row["employee_id"], row["days"]) == ("approved", 8, 3, 2)
    assert "approved" in result["message"]


def test_leave_can_be_applied_for_hr(database):
    service.apply_leave(ADMIN, "EMP-1", "casual", date(2026, 11, 12), None, "full", None)
    assert database.saved_leaves[0]["employee_id"] == 1


def test_leave_still_follows_the_balance(database):
    with pytest.raises(LeaveError, match="only"):
        service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 10, 5), date(2026, 10, 13), "full", None)    # 7 working days, 5 left


@pytest.mark.parametrize("start, end", [(date(2026, 11, 7), None), (date(2026, 11, 9), date(2026, 11, 8)), (date(2026, 12, 25), None)])
def test_leave_cannot_start_or_end_on_a_week_off_or_a_holiday(database, start, end):
    with pytest.raises(LeaveError):
        service.apply_leave(ADMIN, "EMP-3", "casual", start, end, "full", None)
    assert database.saved_leaves == []


def test_leave_for_an_unknown_person(database):
    with pytest.raises(LeaveError) as error:
        service.apply_leave(ADMIN, "EMP-99", "casual", date(2026, 11, 12), None, "full", None)
    assert error.value.status_code == 404


def test_attendance_on_an_old_day_is_saved_and_evaluated(database):
    result = service.mark_attendance(ADMIN, "EMP-3", date(2026, 6, 10), "10:50", "19:05")
    row = database.get_attendance(3, "2026-06-10")
    assert (row["source"], row["status"], row["is_late"]) == ("superadmin", "present", True)
    assert result["status"] == "present"


def test_attendance_replaces_the_times_of_that_day(database):
    service.mark_attendance(ADMIN, "EMP-3", date(2026, 11, 10), "10:00", "14:00")
    service.mark_attendance(ADMIN, "EMP-3", date(2026, 11, 10), None, "19:00")
    row = database.get_attendance(3, "2026-11-10")
    assert (row["status"], row["worked_minutes"]) == ("present", 540)


@pytest.mark.parametrize("day", [date(2026, 11, 7), date(2026, 11, 8), date(2026, 12, 25)])
def test_attendance_cannot_be_set_on_a_week_off_or_a_holiday(database, day):
    with pytest.raises(AttendanceError):
        service.mark_attendance(ADMIN, "EMP-3", day, "10:00", "19:00")


def test_attendance_rules_for_the_day_and_the_person(database):
    with pytest.raises(AttendanceError, match="not come"):
        service.mark_attendance(ADMIN, "EMP-3", date(2026, 11, 13), "10:00", None)
    with pytest.raises(AttendanceError, match="no attendance"):
        service.mark_attendance(ADMIN, "EMP-5", date(2026, 11, 10), "10:00", None)      # the CEO
    with pytest.raises(AttendanceError, match="give the check-in"):
        service.mark_attendance(ADMIN, "EMP-3", date(2026, 11, 10), None, None)
    with pytest.raises(AttendanceError, match="check-in"):
        service.mark_attendance(ADMIN, "EMP-3", date(2026, 11, 10), None, "19:00")      # no check-in saved for that day yet


def test_the_super_admin_sees_everyone(database):
    assert attendance_service.get_access_level(ADMIN) == "superadmin"
    assert {person["emp_id"] for person in attendance_service.list_visible_people(ADMIN)} >= {"EMP-1", "EMP-3", "EMP-7"}
    assert attendance_service.can_view(ADMIN, HR) is True and attendance_service.can_view(HR, ADMIN) is False
    assert attendance_service.list_correction_inbox(ADMIN) == []          # the team pages work


def test_the_super_admin_has_no_attendance_or_leave_of_its_own(database):
    for action in (lambda: attendance_service.get_today_state(ADMIN), lambda: attendance_service.check_in(ADMIN, "1.2.3.4"),
                   lambda: attendance_service.get_my_month(ADMIN, None), lambda: attendance_service.list_my_corrections(ADMIN),
                   lambda: attendance_service.create_correction(ADMIN, date(2026, 11, 10), "10:00", "19:00", "forgot")):
        with pytest.raises(AttendanceError) as error:
            action()
        assert error.value.status_code == 403
    with pytest.raises(LeaveError) as leave_error:
        leave_service.refuse_superadmin(ADMIN)
    assert leave_error.value.status_code == 403


def test_nothing_can_be_saved_for_the_super_admin_account(database):
    assert "EMP-8" not in {person["emp_id"] for person in service.list_employees(ADMIN)}
    with pytest.raises(LeaveError):
        service.apply_leave(ADMIN, "EMP-8", "casual", date(2026, 11, 12), None, "full", None)
    with pytest.raises(AttendanceError, match="no attendance"):
        service.mark_attendance(ADMIN, "EMP-8", date(2026, 11, 10), "10:00", None)


def test_a_confidential_super_admin_keeps_the_team_pages(database):
    assert attendance_service.get_access_level(dict(ADMIN, is_confidential=True)) == "superadmin"


# ------------------------------------------------------------------ the checks, said about the employee
def test_a_zero_balance_gives_a_clear_message_with_the_name(database, monkeypatch):
    monkeypatch.setattr(leave_store, "get_leave_balances", lambda employee_id, on_date: [{"type_code": "casual", "remaining_days": 0}])
    with pytest.raises(LeaveError) as error:
        service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 12), None, "full", None)
    assert str(error.value) == "Ann Employee has only 0 days of Casual Leave left for FY 2026-27, and this request needs 1 day."
    assert database.saved_leaves == []


def test_the_balance_is_checked_for_the_leave_year_of_the_start_date(database, monkeypatch):
    asked = []
    monkeypatch.setattr(leave_store, "get_leave_balances", lambda employee_id, on_date: asked.append(on_date) or [{"type_code": "casual", "remaining_days": 5}])
    service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 2, 3), None, "full", None)
    assert asked == [date(2026, 2, 3)]


def test_the_page_can_ask_for_the_balances(database):
    assert service.get_leave_balances(ADMIN, "EMP-3", date(2026, 11, 12))[0]["remaining_days"] == 5
    with pytest.raises(LeaveError):
        service.get_leave_balances(HR, "EMP-3", date(2026, 11, 12))
    with pytest.raises(LeaveError):
        service.get_leave_balances(ADMIN, "EMP-99", date(2026, 11, 12))


def test_an_overlapping_leave_names_the_employee(database, monkeypatch):
    monkeypatch.setattr(leave_store, "find_overlapping_requests", lambda *args: [{"status": "approved", "leave_type": "casual", "start_date": "2026-11-12", "end_date": "2026-11-13"}])
    with pytest.raises(LeaveError, match="Ann Employee already has a approved casual leave"):
        service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 12), None, "full", None)


def test_a_leave_type_for_the_other_gender_is_refused(database, monkeypatch):
    monkeypatch.setitem(CASUAL, "applies_to_gender", "Female")
    try:
        with pytest.raises(LeaveError, match="not available for Ann Employee"):
            service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 12), None, "full", None)
    finally:
        CASUAL["applies_to_gender"] = None


def test_the_longest_request_and_a_bad_end_date_are_refused(database, monkeypatch):
    monkeypatch.setitem(CASUAL, "max_days_per_request", 2)
    with pytest.raises(LeaveError, match="at most 2"):
        service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 9), date(2026, 11, 12), "full", None)
    with pytest.raises(LeaveError):
        service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 12), date(2026, 11, 10), "full", None)


# ------------------------------------------------------------------ the audit trail
def test_applying_leave_writes_an_audit_entry(database):
    service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 12), None, "full", "forgot to apply")
    entry = database.audit_entries[0]
    assert (entry["actor_emp_id"], entry["target_emp_id"], entry["action"]) == ("EMP-8", "EMP-3", "leave_applied")
    assert entry["details"]["leave_type"] == "casual" and entry["details"]["reason"] == "forgot to apply"


def test_marking_attendance_writes_an_audit_entry(database):
    service.mark_attendance(ADMIN, "EMP-3", date(2026, 11, 10), "10:00", "19:00")
    entry = database.audit_entries[0]
    assert (entry["actor_emp_id"], entry["target_emp_id"], entry["action"]) == ("EMP-8", "EMP-3", "attendance_set")
    assert entry["details"]["work_date"] == "2026-11-10" and entry["details"]["status"] == "present"


def test_a_failed_attempt_leaves_no_audit_entry(database):
    with pytest.raises(LeaveError):
        service.apply_leave(ADMIN, "EMP-99", "casual", date(2026, 11, 12), None, "full", None)
    assert database.audit_entries == []


def test_an_audit_logging_problem_never_blocks_the_save(database, monkeypatch):
    def broken_insert(fields):
        raise RuntimeError("the database is unreachable")
    monkeypatch.setattr(superadmin_store, "insert_audit_entry", broken_insert)
    result = service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 12), None, "full", None)
    assert "approved" in result["message"]
    assert database.saved_leaves[0]["employee_id"] == 3


def test_only_the_super_admin_may_read_the_audit_log(database):
    with pytest.raises(LeaveError) as error:
        service.list_audit_log(HR)
    assert error.value.status_code == 403


def test_the_audit_log_is_newest_first(database):
    service.apply_leave(ADMIN, "EMP-3", "casual", date(2026, 11, 12), None, "full", None)
    service.mark_attendance(ADMIN, "EMP-3", date(2026, 11, 10), "10:00", "19:00")
    log = service.list_audit_log(ADMIN)
    assert [entry["action"] for entry in log] == ["attendance_set", "leave_applied"]
