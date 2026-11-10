"""Tests for app/leave_service.py (the leave rules). The database is the in-memory FakeDatabase."""
from datetime import date

import pytest

from app.hr.leave import service as leave_service
from fakes import FakeDatabase
from app.hr.leave.service import LeaveError

TODAY = date(2026, 11, 7)          # a Saturday, the example date used when the rules were agreed


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setattr(leave_service, "get_today", lambda: TODAY)
    return FakeDatabase().install(monkeypatch)


def check(database, employee_id=3, leave_type="casual", start=date(2026, 11, 10), end=None, day_type="full", chat=True, document=None):
    return leave_service.check_new_leave(database.employees[employee_id], leave_type, start, end or start, day_type, chat, document)


def test_a_normal_single_day_leave_is_accepted(database):
    details = check(database)
    assert details["days"] == 1
    assert details["leave_type"] == "casual"


def test_a_range_skips_weekends_and_holidays(database):
    details = check(database, start=date(2026, 11, 12), end=date(2026, 11, 18))     # Thu to Wed, 5 working days
    assert details["days"] == 5


def test_past_dates_are_allowed_only_seven_days_back(database):
    check(database, start=date(2026, 11, 2))                                      # 5 days back: allowed
    with pytest.raises(LeaveError, match="up to 7 days back"):
        check(database, start=date(2026, 10, 22))                                 # 16 days back: refused


def test_one_request_cannot_cover_more_than_seven_days(database):
    with pytest.raises(LeaveError, match="at most 7 consecutive days"):
        check(database, start=date(2026, 11, 9), end=date(2026, 11, 17))
    check(database, start=date(2026, 11, 9), end=date(2026, 11, 15))                # exactly 7 dates is fine


def test_a_weekend_or_holiday_needs_no_leave(database):
    with pytest.raises(LeaveError, match="weekend or a company holiday"):
        check(database, start=date(2026, 11, 14))                                 # Saturday
    with pytest.raises(LeaveError, match="weekend or a company holiday"):
        check(database, start=date(2026, 12, 25))                                 # holiday


def test_a_half_day_must_be_a_single_date_and_costs_half(database):
    assert check(database, day_type="half")["days"] == 0.5
    with pytest.raises(LeaveError, match="single date"):
        check(database, start=date(2026, 11, 10), end=date(2026, 11, 11), day_type="half")


def test_end_before_start_is_refused(database):
    with pytest.raises(LeaveError, match="before the start"):
        check(database, start=date(2026, 11, 11), end=date(2026, 11, 10))


def test_the_yearly_balance_is_enforced(database):
    database.add_leave(3, "casual", "2026-06-01", "2026-06-05", 5, status="approved")
    database.add_leave(3, "casual", "2026-04-06", "2026-04-07", 2, status="pending")
    check(database)                                                               # 1 day left, 1 day asked: fine
    with pytest.raises(LeaveError, match="only 1 day of Casual Leave left for FY 2026-27"):
        check(database, start=date(2026, 11, 10), end=date(2026, 11, 11))


def test_overlapping_leave_is_refused(database):
    database.add_leave(3, "sick", "2026-11-10", "2026-11-11", 2, status="pending")
    with pytest.raises(LeaveError, match="overlaps"):
        check(database, start=date(2026, 11, 11))


def test_rejected_and_cancelled_leaves_do_not_block_new_ones(database):
    database.add_leave(3, "sick", "2026-11-10", "2026-11-10", 1, status="rejected")
    check(database)


def test_unknown_leave_type_lists_the_choices(database):
    with pytest.raises(LeaveError, match="casual, sick"):
        check(database, leave_type="holiday")


def test_maternity_rules(database):
    with pytest.raises(LeaveError, match="cannot be applied in the chat"):
        check(database, leave_type="maternity", chat=True)
    with pytest.raises(LeaveError, match="needs a supporting document"):
        check(database, leave_type="maternity", chat=False, end=date(2026, 11, 10))
    with pytest.raises(LeaveError, match="not available for you"):
        check(database, employee_id=4, leave_type="maternity", chat=False, document="4/x.pdf")        # a male employee
    details = check(database, leave_type="maternity", start=date(2026, 11, 10), end=date(2027, 2, 7), chat=False, document="3/x.pdf")
    assert details["days"] == 90                                                  # calendar days, no 7 day limit
    with pytest.raises(LeaveError, match="at most 90 days"):
        check(database, leave_type="maternity", start=date(2026, 11, 10), end=date(2027, 2, 8), chat=False, document="3/x.pdf")


def test_an_inactive_employee_cannot_apply(database):
    database.employees[3]["is_active"] = False
    with pytest.raises(LeaveError):
        check(database)


# ------------------------------------------------------------------ saving, approving, cancelling
def test_apply_on_website_saves_a_pending_request_for_the_manager(database):
    result = leave_service.apply_on_website(database.employees[3], "casual", date(2026, 11, 10), None, "full", "family", None)
    assert result["leave"]["status"] == "pending"
    assert result["approver_name"] == "Rahul Verma"


def test_maternity_goes_to_hr_and_a_foreign_document_is_refused(database):
    with pytest.raises(LeaveError, match="does not belong to you"):
        leave_service.apply_on_website(database.employees[3], "maternity", date(2026, 11, 10), date(2027, 2, 7), "full", None, "5/other.pdf")
    result = leave_service.apply_on_website(database.employees[3], "maternity", date(2026, 11, 10), date(2027, 2, 7), "full", None, "3/mine.pdf")
    assert result["approver_name"] == "HR"


def hr(employee_id=1):
    return {"employee_id": employee_id, "role": "hr"}


def staff(employee_id, dept="IT"):
    return {"employee_id": employee_id, "role": "employee", "dept": dept}


def pm(employee_id=6):
    return staff(employee_id, dept="PM")


def test_the_reporting_manager_and_hr_can_approve(database):
    first = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    second = database.add_leave(4, "casual", "2026-11-11", "2026-11-11", 1, status="pending")
    assert leave_service.decide_leave(staff(2), first["id"], "approved")["status"] == "approved"
    assert leave_service.decide_leave(hr(), second["id"], "rejected", "busy week")["status"] == "rejected"


def test_nobody_else_can_decide(database):
    leave = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    with pytest.raises(LeaveError, match="Only the reporting manager"):
        leave_service.decide_leave(staff(4), leave["id"], "approved")             # a colleague
    with pytest.raises(LeaveError, match="your own leave"):
        leave_service.decide_leave(staff(3), leave["id"], "approved")             # the employee themselves


def test_hr_cannot_approve_their_own_leave(database):
    leave = database.add_leave(1, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    with pytest.raises(LeaveError, match="your own leave"):
        leave_service.decide_leave(hr(1), leave["id"], "approved")


def test_maternity_is_decided_by_hr_only(database):
    leave = database.add_leave(3, "maternity", "2026-11-10", "2027-02-07", 90, status="pending", document_path="3/a.pdf")
    with pytest.raises(LeaveError, match="Only the reporting manager"):
        leave_service.decide_leave(staff(2), leave["id"], "approved")
    assert leave_service.decide_leave(hr(), leave["id"], "approved")["status"] == "approved"


def test_a_rejection_must_say_why(database):
    leave = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    for empty_note in (None, "", "   "):
        with pytest.raises(LeaveError, match="reason for rejecting"):
            leave_service.decide_leave(staff(2), leave["id"], "rejected", empty_note)
    assert database.leave_requests[0]["status"] == "pending"            # nothing was changed
    assert leave_service.decide_leave(staff(2), leave["id"], "approved")["status"] == "approved"   # approving needs no reason


def test_a_decided_leave_cannot_be_decided_again(database):
    leave = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    leave_service.decide_leave(staff(2), leave["id"], "approved")
    with pytest.raises(LeaveError, match="already approved"):
        leave_service.decide_leave(staff(2), leave["id"], "rejected")


def test_the_approval_list_matches_who_may_decide(database):
    database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")                  # Rahul's report
    database.add_leave(5, "sick", "2026-11-11", "2026-11-11", 1, status="pending")                    # reports to Priya (HR)
    database.add_leave(3, "maternity", "2027-01-04", "2027-04-03", 90, status="pending", document_path="3/a.pdf")
    rahul = [item["emp_id"] + ":" + item["leave_type"] for item in leave_service.list_approvals(staff(2))]
    assert rahul == ["EMP003:casual"]                                              # not the maternity request, not other teams
    assert len(leave_service.list_approvals(hr(1))) == 3
    assert leave_service.list_approvals(staff(4)) == []


def test_cancelling(database):
    pending = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    assert leave_service.cancel_leave(database.employees[3], pending["id"])["status"] == "cancelled"

    started = database.add_leave(3, "casual", "2026-11-02", "2026-11-02", 1, status="approved")       # already in the past
    with pytest.raises(LeaveError, match="cannot be cancelled"):
        leave_service.cancel_leave(database.employees[3], started["id"])

    future = database.add_leave(3, "casual", "2026-11-20", "2026-11-20", 1, status="approved")
    assert leave_service.cancel_leave(database.employees[3], future["id"])["status"] == "cancelled"

    with pytest.raises(LeaveError, match="could not find"):
        leave_service.cancel_leave(database.employees[4], future["id"])           # someone else's leave


def test_documents_can_be_read_by_the_owner_the_manager_and_hr_only(database):
    leave = database.add_leave(3, "sick", "2026-11-10", "2026-11-10", 1, status="pending", document_path="3/note.pdf")
    for reader in (staff(3), staff(2), hr()):
        assert leave_service.check_can_read_document(reader, leave["id"]) == "3/note.pdf"
    with pytest.raises(LeaveError, match="not allowed"):
        leave_service.check_can_read_document(staff(4), leave["id"])
    maternity = database.add_leave(3, "maternity", "2027-01-04", "2027-04-03", 90, status="pending", document_path="3/m.pdf")
    with pytest.raises(LeaveError, match="not allowed"):
        leave_service.check_can_read_document(staff(2), maternity["id"])          # the manager must not see medical papers


def test_attaching_a_document_to_the_waiting_leave(database):
    action = database.create_pending_action(3, "apply_leave", {"leave_type": "sick"}, "Sick Leave")
    leave_service.attach_document_to_action(database.employees[3], action["id"], "3/note.pdf")
    assert action["payload"]["document_path"] == "3/note.pdf"
    with pytest.raises(LeaveError, match="does not belong to you"):
        leave_service.attach_document_to_action(database.employees[3], action["id"], "4/note.pdf")
    with pytest.raises(LeaveError, match="no leave waiting"):
        leave_service.attach_document_to_action(database.employees[4], action["id"], "4/note.pdf")


def test_leave_year_runs_from_april_to_march(database):
    assert leave_service.leave_year_label(date(2026, 11, 9)) == "FY 2026-27"
    assert leave_service.leave_year_label(date(2027, 3, 31)) == "FY 2026-27"
    assert leave_service.leave_year_label(date(2027, 4, 1)) == "FY 2027-28"
    assert leave_service.leave_year_label(date(2026, 3, 31)) == "FY 2025-26"


def test_last_years_leave_does_not_use_this_years_balance(database):
    database.add_leave(3, "casual", "2026-02-02", "2026-02-06", 5, status="approved")        # FY 2025-26
    details = check(database, start=date(2026, 11, 9), end=date(2026, 11, 13))               # 5 days; all 8 of this year are free
    assert details["days"] == 5


# ------------------------------------------------------------------ the PM approves when there is no manager_id
def test_without_a_manager_the_pm_is_the_approver(database):
    result = leave_service.apply_on_website(database.employees[7], "casual", date(2026, 11, 10), None, "full", None, None)
    assert result["approver_name"] == "Sam Roy"
    leave_id = result["leave"]["id"]
    with pytest.raises(LeaveError, match="Only the reporting manager"):
        leave_service.decide_leave(staff(2), leave_id, "approved")                         # another manager cannot
    assert leave_service.decide_leave(pm(), leave_id, "approved")["status"] == "approved"


def test_hr_can_still_approve_when_there_is_no_manager(database):
    leave = database.add_leave(7, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    assert leave_service.decide_leave(hr(), leave["id"], "rejected", "no cover that day")["status"] == "rejected"


def test_the_pm_does_not_override_a_real_manager(database):
    leave = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")      # Ananya has manager 2
    with pytest.raises(LeaveError, match="Only the reporting manager"):
        leave_service.decide_leave(pm(), leave["id"], "approved")


def test_the_pms_own_leave_is_for_hr_only(database):
    result = leave_service.apply_on_website(database.employees[6], "casual", date(2026, 11, 10), None, "full", None, None)
    assert result["approver_name"] == "HR"
    leave_id = result["leave"]["id"]
    with pytest.raises(LeaveError, match="your own leave"):
        leave_service.decide_leave(pm(), leave_id, "approved")
    with pytest.raises(LeaveError, match="Only the reporting manager"):
        leave_service.decide_leave(staff(2), leave_id, "approved")
    assert leave_service.decide_leave(hr(), leave_id, "approved")["status"] == "approved"


def test_the_pm_sees_only_unmanaged_employees_leaves(database):
    database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")      # has a manager: not for the PM
    database.add_leave(7, "sick", "2026-11-11", "2026-11-11", 1, status="pending")        # no manager: for the PM
    database.add_leave(2, "sick", "2026-11-12", "2026-11-12", 1, status="pending")        # Rahul has no manager either
    assert sorted(item["emp_id"] for item in leave_service.list_approvals(pm())) == ["EMP002", "EMP007"]


def test_the_pm_can_open_documents_of_unmanaged_employees_only(database):
    mine = database.add_leave(7, "sick", "2026-11-11", "2026-11-11", 1, status="pending", document_path="7/a.pdf")
    other = database.add_leave(3, "sick", "2026-11-12", "2026-11-12", 1, status="pending", document_path="3/a.pdf")
    assert leave_service.check_can_read_document(pm(), mine["id"]) == "7/a.pdf"
    with pytest.raises(LeaveError, match="not allowed"):
        leave_service.check_can_read_document(pm(), other["id"])


# ------------------------------------------------------------------ the pm login role
def test_a_pm_login_role_approves_unmanaged_leave_without_hr_powers(database):
    pm_login = {"employee_id": 6, "role": "pm", "dept": "Operations"}      # role pm counts even if the dept text differs
    leave = database.add_leave(7, "casual", "2026-11-10", "2026-11-10", 1, status="pending")           # Dev Paul has no manager
    managed = database.add_leave(3, "casual", "2026-11-11", "2026-11-11", 1, status="pending")         # Ananya has a manager
    assert [row["id"] for row in leave_service.list_approvals(pm_login)] == [leave["id"]]
    with pytest.raises(LeaveError, match="Only the reporting manager"):
        leave_service.decide_leave(pm_login, managed["id"], "approved")
    assert leave_service.decide_leave(pm_login, leave["id"], "approved")["status"] == "approved"


def test_the_pm_role_can_be_given_to_a_login():
    from app.identity import auth as auth
    assert "pm" in auth.ALLOWED_ROLES
