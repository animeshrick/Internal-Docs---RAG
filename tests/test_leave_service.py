"""Tests for app/leave_service.py (the leave rules). The database is the in-memory FakeDatabase."""
from datetime import date, timedelta

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
def test_apply_on_website_sends_every_leave_to_the_super_admin(database):
    result = leave_service.apply_on_website(database.employees[3], "casual", date(2026, 11, 10), None, "full", "family", None)
    assert result["leave"]["status"] == "pending"
    assert result["approver_name"] == "the super admin"


def test_maternity_also_goes_to_the_super_admin_and_a_foreign_document_is_refused(database):
    with pytest.raises(LeaveError, match="does not belong to you"):
        leave_service.apply_on_website(database.employees[3], "maternity", date(2026, 11, 10), date(2027, 2, 7), "full", None, "5/other.pdf")
    result = leave_service.apply_on_website(database.employees[3], "maternity", date(2026, 11, 10), date(2027, 2, 7), "full", None, "3/mine.pdf")
    assert result["approver_name"] == "the super admin"


def hr(employee_id=1):
    return {"employee_id": employee_id, "role": "hr"}


def staff(employee_id, dept="IT"):
    return {"employee_id": employee_id, "role": "employee", "dept": dept}


def pm(employee_id=6):
    return staff(employee_id, dept="PM")


def superadmin(employee_id=9):
    return {"employee_id": employee_id, "role": "superadmin"}


def test_only_the_superadmin_can_approve_leave_not_the_manager_or_hr(database):
    """Every leave type is decided only by the super admin now - not the reporting manager, not HR."""
    first = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")          # Ananya, manager is Rahul (2)
    second = database.add_leave(4, "casual", "2026-11-11", "2026-11-11", 1, status="pending")
    for approver in (staff(2), hr()):
        with pytest.raises(LeaveError, match="Only the super admin"):
            leave_service.decide_leave(approver, first["id"], "approved")
    assert leave_service.decide_leave(superadmin(), first["id"], "approved")["status"] == "approved"
    assert leave_service.decide_leave(superadmin(), second["id"], "rejected", "busy week")["status"] == "rejected"


def test_nobody_else_can_decide(database):
    leave = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    for approver in (staff(4), staff(2), hr(), pm()):                             # a colleague, the manager, HR, the PM
        with pytest.raises(LeaveError, match="Only the super admin"):
            leave_service.decide_leave(approver, leave["id"], "approved")
    with pytest.raises(LeaveError, match="your own leave"):
        leave_service.decide_leave(staff(3), leave["id"], "approved")             # the employee themselves


def test_hr_is_treated_like_any_other_employee_for_its_own_leave(database):
    """HR gets no shortcut: its own leave is not auto-approved, and HR itself cannot decide it either - only
    the super admin can (and still not HR itself, even logged in as HR, over its own request)."""
    leave = database.add_leave(1, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    assert leave["status"] == "pending"                                           # not auto-approved
    with pytest.raises(LeaveError, match="your own leave"):
        leave_service.decide_leave(hr(1), leave["id"], "approved")
    assert leave_service.decide_leave(superadmin(), leave["id"], "approved")["status"] == "approved"


def test_maternity_is_decided_by_the_super_admin_only(database):
    leave = database.add_leave(3, "maternity", "2026-11-10", "2027-02-07", 90, status="pending", document_path="3/a.pdf")
    for approver in (staff(2), hr()):
        with pytest.raises(LeaveError, match="Only the super admin"):
            leave_service.decide_leave(approver, leave["id"], "approved")
    assert leave_service.decide_leave(superadmin(), leave["id"], "approved")["status"] == "approved"


def test_a_rejection_must_say_why(database):
    leave = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    for empty_note in (None, "", "   "):
        with pytest.raises(LeaveError, match="reason for rejecting"):
            leave_service.decide_leave(superadmin(), leave["id"], "rejected", empty_note)
    assert database.leave_requests[0]["status"] == "pending"            # nothing was changed
    assert leave_service.decide_leave(superadmin(), leave["id"], "approved")["status"] == "approved"   # approving needs no reason


def test_a_decided_leave_cannot_be_decided_again(database):
    leave = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    leave_service.decide_leave(superadmin(), leave["id"], "approved")
    with pytest.raises(LeaveError, match="already approved"):
        leave_service.decide_leave(superadmin(), leave["id"], "rejected")


def test_the_approval_list_is_empty_for_everyone_but_the_super_admin(database):
    database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")                  # Rahul's report
    database.add_leave(5, "sick", "2026-11-11", "2026-11-11", 1, status="pending")                    # reports to Priya (HR)
    database.add_leave(3, "maternity", "2027-01-04", "2027-04-03", 90, status="pending", document_path="3/a.pdf")
    assert leave_service.list_approvals(staff(2)) == []
    assert leave_service.list_approvals(hr(1)) == []
    assert leave_service.list_approvals(pm()) == []
    assert leave_service.list_approvals(staff(4)) == []
    assert len(leave_service.list_approvals(superadmin())) == 3


def test_cancelling(database):
    """Only a still-pending leave can be self-cancelled. Once the super admin has decided it - approved
    or rejected, started or not - the employee can no longer undo it themselves."""
    pending = database.add_leave(3, "casual", "2026-11-10", "2026-11-10", 1, status="pending")
    assert leave_service.cancel_leave(database.employees[3], pending["id"])["status"] == "cancelled"

    started = database.add_leave(3, "casual", "2026-11-02", "2026-11-02", 1, status="approved")       # already in the past
    with pytest.raises(LeaveError, match="cannot be cancelled"):
        leave_service.cancel_leave(database.employees[3], started["id"])

    future = database.add_leave(3, "casual", "2026-11-20", "2026-11-20", 1, status="approved")         # approved, not started yet
    with pytest.raises(LeaveError, match="cannot be cancelled"):
        leave_service.cancel_leave(database.employees[3], future["id"])

    with pytest.raises(LeaveError, match="could not find"):
        leave_service.cancel_leave(database.employees[4], future["id"])           # someone else's leave


def test_documents_can_be_read_by_the_owner_and_the_super_admin_only(database):
    leave = database.add_leave(3, "sick", "2026-11-10", "2026-11-10", 1, status="pending", document_path="3/note.pdf")
    assert leave_service.check_can_read_document(staff(3), leave["id"]) == "3/note.pdf"       # the owner
    assert leave_service.check_can_read_document(superadmin(), leave["id"]) == "3/note.pdf"
    for reader in (staff(2), hr(), staff(4)):                                     # the manager, HR, a colleague: none of them
        with pytest.raises(LeaveError, match="not allowed"):
            leave_service.check_can_read_document(reader, leave["id"])


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


# ------------------------------------------------------------------ the PM and an unmanaged employee's leave
# (the PM used to approve these; now the PM has no leave-decision power at all - see can_decide)
def test_an_employee_with_no_manager_also_only_goes_to_the_super_admin(database):
    result = leave_service.apply_on_website(database.employees[7], "casual", date(2026, 11, 10), None, "full", None, None)
    assert result["approver_name"] == "the super admin"
    leave_id = result["leave"]["id"]
    with pytest.raises(LeaveError, match="Only the super admin"):
        leave_service.decide_leave(pm(), leave_id, "approved")
    assert leave_service.decide_leave(superadmin(), leave_id, "approved")["status"] == "approved"


def test_a_pm_login_role_has_no_special_leave_decision_power(database):
    pm_login = {"employee_id": 6, "role": "pm", "dept": "Operations"}      # role pm counts even if the dept text differs
    leave = database.add_leave(7, "casual", "2026-11-10", "2026-11-10", 1, status="pending")           # Dev Paul has no manager
    assert leave_service.list_approvals(pm_login) == []
    with pytest.raises(LeaveError, match="Only the super admin"):
        leave_service.decide_leave(pm_login, leave["id"], "approved")


def test_the_pm_role_can_be_given_to_a_login():
    from app.identity import auth as auth
    assert "pm" in auth.ALLOWED_ROLES


# ------------------------------------------------------------------ WFH: only the super admin may decide it
def test_wfh_approver_name_is_the_super_admin(database):
    result = leave_service.apply_on_website(database.employees[3], "wfh", date(2026, 11, 10), None, "full", None, None)
    assert result["approver_name"] == "the super admin"


def test_wfh_needs_no_document(database):
    details = check(database, leave_type="wfh")           # the default check() helper passes document=None
    assert details["leave_type"] == "wfh"


def test_wfh_has_no_yearly_limit(database):
    """limit_type is "unlimited": approving well past the old 12-day quota still succeeds (every
    request still needs the super admin's approval - see test_only_the_superadmin_can_decide_wfh)."""
    for month_start in (date(2026, 4, 1), date(2026, 5, 4), date(2026, 6, 1)):          # three separate 5-day weeks
        database.add_leave(3, "wfh", month_start.isoformat(), (month_start + timedelta(days=4)).isoformat(), 5, status="approved")
    details = check(database, leave_type="wfh", start=date(2026, 11, 10), end=date(2026, 11, 11))          # well past 15 days used already
    assert details["days"] == 2


def test_only_the_superadmin_can_decide_wfh(database):
    leave = database.add_leave(3, "wfh", "2026-11-10", "2026-11-10", 1, status="pending")             # Ananya, manager is Rahul (2)
    with pytest.raises(LeaveError, match="Only the super admin"):
        leave_service.decide_leave(staff(2), leave["id"], "approved")          # the reporting manager: refused
    with pytest.raises(LeaveError, match="Only the super admin"):
        leave_service.decide_leave(hr(), leave["id"], "approved")              # plain HR: refused, even though HR decides everything else
    with pytest.raises(LeaveError, match="Only the super admin"):
        leave_service.decide_leave(pm(), leave["id"], "approved")              # the PM: refused
    assert leave_service.decide_leave(superadmin(), leave["id"], "approved")["status"] == "approved"


def test_superadmin_cannot_approve_their_own_wfh(database):
    leave = database.add_leave(9, "wfh", "2026-11-10", "2026-11-10", 1, status="pending")
    with pytest.raises(LeaveError, match="your own leave"):
        leave_service.decide_leave(superadmin(9), leave["id"], "approved")


def test_hr_sees_no_pending_leave_of_any_type_only_the_superadmin_does(database):
    wfh = database.add_leave(3, "wfh", "2026-11-10", "2026-11-10", 1, status="pending")
    casual = database.add_leave(4, "casual", "2026-11-11", "2026-11-11", 1, status="pending")
    assert leave_service.list_approvals(hr()) == []
    superadmin_ids = [item["id"] for item in leave_service.list_approvals(superadmin())]
    assert wfh["id"] in superadmin_ids
    assert casual["id"] in superadmin_ids


def test_wfh_is_not_decided_by_a_manager_even_without_approver_override(database):
    leave = database.add_leave(4, "wfh", "2026-11-10", "2026-11-10", 1, status="pending")              # Karan, manager is Rahul (2)
    assert leave_service.list_approvals(staff(2)) == []     # Rahul's own team's WFH request does not show up for him


# ------------------------------------------------------------------ Sick Leave: a document is needed only above 1 day
def test_sick_leave_of_one_day_or_less_needs_no_document(database):
    details = check(database, leave_type="sick", start=date(2026, 11, 10), end=date(2026, 11, 10))       # Tue, 1 day
    assert details["leave_type"] == "sick"
    half_day = check(database, leave_type="sick", start=date(2026, 11, 11), end=date(2026, 11, 11), day_type="half")
    assert half_day["days"] == 0.5


def test_sick_leave_of_more_than_one_day_needs_a_document(database):
    with pytest.raises(LeaveError, match="needs a supporting document"):
        check(database, leave_type="sick", start=date(2026, 11, 10), end=date(2026, 11, 11))              # Tue-Wed, 2 days, no document


def test_sick_leave_of_more_than_one_day_is_accepted_with_a_document(database):
    details = check(database, leave_type="sick", start=date(2026, 11, 10), end=date(2026, 11, 11), document="3/sick-note.pdf")
    assert details["days"] == 2


def test_the_chat_preview_does_not_demand_a_document_before_one_is_offered(database):
    """handle_apply_leave in the chat assistant passes require_document=False for the preview step; the final
    confirmation (run_confirmed_action/apply_on_website) is where the document is actually required."""
    details = leave_service.check_new_leave(database.employees[3], "sick", date(2026, 11, 10), date(2026, 11, 11), "full",
                                            applied_by_chat=True, require_document=False)
    assert details["leave_type"] == "sick"


def test_sick_leave_website_submission_is_refused_without_a_document_above_the_threshold(database):
    with pytest.raises(LeaveError, match="needs a supporting document"):
        leave_service.apply_on_website(database.employees[3], "sick", date(2026, 11, 10), date(2026, 11, 11), "full", None, None)


def test_sick_leave_website_submission_succeeds_with_a_document_above_the_threshold(database):
    result = leave_service.apply_on_website(database.employees[3], "sick", date(2026, 11, 10), date(2026, 11, 11), "full",
                                            None, "3/sick-note.pdf")
    assert result["leave"]["leave_type"] == "sick"
