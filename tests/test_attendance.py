"""Tests for the attendance rules (app/hr/attendance/service.py). The database is the in-memory FakeAttendanceDatabase."""
from datetime import date

import pytest

from app.hr.attendance import service
from app.hr.attendance.service import AttendanceError
from fakes_attendance import FakeAttendanceDatabase, india, login_of

HR, PM, EMPLOYEE, SALES, CEO, CTO, OTHER_HR = (login_of(person_id) for person_id in (1, 2, 3, 4, 5, 6, 7))
LOCAL_IP = "203.0.113.7"


@pytest.fixture
def database(monkeypatch):
    return FakeAttendanceDatabase().install(monkeypatch)


def work_day(database, check_in_clock, check_out_clock=None, day="2026-11-12", employee=EMPLOYEE):
    """Check in and (optionally) out at the given India times. Returns the saved row."""
    database.set_time(check_in_clock, day)
    service.check_in(employee, LOCAL_IP)
    if check_out_clock:
        database.set_time(check_out_clock, day)
        service.check_out(employee, LOCAL_IP)
    return database.get_attendance(employee["employee_id"], day)


# ------------------------------------------------------------------ how a day counts
def evaluate(database, in_clock, out_clock):
    settings = service.read_settings()
    return service.evaluate_day(india("2026-11-12", in_clock), india("2026-11-12", out_clock) if out_clock else None, settings)


def test_late_only_after_the_45_minute_buffer(database):
    assert evaluate(database, "10:45", None)["is_late"] is False
    assert evaluate(database, "10:46", None)["is_late"] is True
    assert evaluate(database, "09:30", None)["is_late"] is False


@pytest.mark.parametrize("in_clock, out_clock, status, early_leave", [
    ("10:00", "19:00", "present", False),
    ("10:00", "19:30", "present", False),
    ("10:00", "18:00", "present", True),       # left after 3 PM but before 7 PM: early leave
    ("10:00", "15:01", "present", True),
    ("10:00", "15:00", "half_day", False),     # at 3 PM exactly is still a half day
    ("10:00", "14:30", "half_day", False),     # 4.5 hours worked
    ("10:00", "14:29", "absent", False),       # less than 4.5 hours
    ("13:00", "17:00", "absent", False),       # came late and worked only 4 hours
    ("10:00", None, "checked_in", False),
])
def test_status_from_the_hours_worked(database, in_clock, out_clock, status, early_leave):
    result = evaluate(database, in_clock, out_clock)
    assert (result["status"], result["is_early_leave"]) == (status, early_leave)


def test_worked_minutes_are_counted(database):
    assert evaluate(database, "10:00", "19:10")["worked_minutes"] == 550


# ------------------------------------------------------------------ check in and check out
def test_check_in_and_out_are_saved_with_the_marks(database):
    row = work_day(database, "10:50", "19:05")
    assert (row["status"], row["is_late"], row["worked_minutes"], row["source"]) == ("present", True, 495, "self")
    state = service.get_today_state(EMPLOYEE)
    assert state["can_check_in"] is False and state["can_check_out"] is False and state["status"] == "present"


def test_the_buttons_follow_the_state_of_the_day(database):
    assert service.get_today_state(EMPLOYEE)["can_check_in"] is True
    database.set_time("10:05")
    service.check_in(EMPLOYEE, LOCAL_IP)
    state = service.get_today_state(EMPLOYEE)
    assert (state["can_check_in"], state["can_check_out"], state["status"]) == (False, True, "checked_in")


def test_a_second_check_in_or_check_out_is_refused(database):
    work_day(database, "10:05")
    with pytest.raises(AttendanceError, match="already checked in today at 10:05") as error:
        service.check_in(EMPLOYEE, LOCAL_IP)
    assert error.value.status_code == 409
    database.set_time("19:00")
    service.check_out(EMPLOYEE, LOCAL_IP)
    with pytest.raises(AttendanceError, match="already checked out today at 19:00"):
        service.check_out(EMPLOYEE, LOCAL_IP)


def test_check_out_needs_a_check_in(database):
    with pytest.raises(AttendanceError, match="not checked in today"):
        service.check_out(EMPLOYEE, LOCAL_IP)


def test_no_check_in_on_a_weekend_a_holiday_or_a_day_of_leave(database):
    database.set_time("10:00", "2026-11-14")                                  # Saturday
    with pytest.raises(AttendanceError, match="weekend"):
        service.check_in(EMPLOYEE, LOCAL_IP)
    database.set_time("10:00", "2026-12-25")
    with pytest.raises(AttendanceError, match="Christmas"):
        service.check_in(EMPLOYEE, LOCAL_IP)
    database.leaves.append({"employee_id": 3, "leave_type": "casual", "start_date": "2026-11-12", "end_date": "2026-11-12", "day_type": "full"})
    database.set_time("10:00")
    with pytest.raises(AttendanceError, match="approved leave"):
        service.check_in(EMPLOYEE, LOCAL_IP)


def test_a_half_day_of_leave_still_allows_a_check_in(database):
    database.leaves.append({"employee_id": 3, "leave_type": "casual", "start_date": "2026-11-12", "end_date": "2026-11-12", "day_type": "half"})
    assert service.check_in(EMPLOYEE, LOCAL_IP)["status"] == "checked_in"


def test_the_ceo_and_the_cto_have_no_attendance(database):
    for person in (CEO, CTO):
        assert service.get_access_level(person) is None
        for action in (lambda p=person: service.check_in(p, LOCAL_IP), lambda p=person: service.get_today_state(p),
                       lambda p=person: service.get_my_month(p, None)):
            with pytest.raises(AttendanceError, match="not available") as error:
                action()
            assert error.value.status_code == 403


def test_the_levels_follow_the_hierarchy(database):
    assert [service.get_access_level(person) for person in (HR, PM, EMPLOYEE, SALES)] == ["hr", "pm", "employee", "employee"]
    assert service.get_access_level({"role": "employee", "dept": "pm"}) == "pm"        # the PM department counts too


# ------------------------------------------------------------------ the office network
def test_the_network_check_is_off_while_testing(database):
    assert service.check_in(EMPLOYEE, "8.8.8.8")["status"] == "checked_in"


def test_with_the_check_on_only_the_office_network_may_check_in(database):
    database.settings["network_check_enabled"] = True
    database.networks = [{"id": 1, "label": "Office", "cidr": "203.0.113.0/24"}]
    with pytest.raises(AttendanceError, match="office network") as error:
        service.check_in(EMPLOYEE, "8.8.8.8")
    assert error.value.status_code == 403
    with pytest.raises(AttendanceError, match="office network"):
        service.check_in(EMPLOYEE, "not an address")
    assert service.check_in(EMPLOYEE, "203.0.113.99")["status"] == "checked_in"
    database.set_time("19:00")
    with pytest.raises(AttendanceError, match="office network"):
        service.check_out(EMPLOYEE, "8.8.8.8")


def test_with_the_check_on_and_no_network_nobody_can_check_in(database):
    database.settings["network_check_enabled"] = True
    with pytest.raises(AttendanceError, match="office network"):
        service.check_in(EMPLOYEE, LOCAL_IP)


# ------------------------------------------------------------------ who sees whom
@pytest.mark.parametrize("viewer, target, allowed", [
    (HR, PM, True), (HR, EMPLOYEE, True), (HR, SALES, True), (HR, OTHER_HR, False), (HR, CEO, False), (HR, CTO, False), (HR, HR, True),
    (PM, EMPLOYEE, True), (PM, SALES, True), (PM, HR, False), (PM, CEO, False), (PM, CTO, False), (PM, PM, True),
    (EMPLOYEE, SALES, False), (EMPLOYEE, PM, False), (EMPLOYEE, HR, False), (EMPLOYEE, EMPLOYEE, True),
    (CEO, EMPLOYEE, False), (CTO, EMPLOYEE, False), (CEO, CEO, False),
])
def test_each_level_sees_only_the_levels_below(database, viewer, target, allowed):
    assert service.can_view(viewer, target) is allowed


def test_the_team_view_for_hr_and_the_pm(database):
    work_day(database, "10:00", "19:00")
    hr_view = service.get_team_day(HR, None)
    assert [person["emp_id"] for person in hr_view["people"]] == ["EMP-2", "EMP-3", "EMP-4"]
    assert [person["status"] for person in hr_view["people"]] == ["not_checked_in", "present", "not_checked_in"]
    assert [person["emp_id"] for person in service.get_team_day(PM, "2026-11-12")["people"]] == ["EMP-3", "EMP-4"]
    with pytest.raises(AttendanceError, match="Only HR and the PM") as error:
        service.get_team_day(EMPLOYEE, None)
    assert error.value.status_code == 403
    with pytest.raises(AttendanceError):
        service.get_team_day(CEO, None)
    with pytest.raises(AttendanceError, match="not come yet"):
        service.get_team_day(HR, "2026-11-13")


def test_one_persons_month_is_visible_only_below(database):
    assert service.get_person_month(HR, "EMP-3", None)["full_name"] == "Ann Employee"
    assert service.get_person_month(PM, "Ann Employee", None)["emp_id"] == "EMP-3"
    for viewer, hidden in ((PM, "EMP-1"), (PM, "EMP-5"), (PM, "EMP-6"), (HR, "EMP-7"), (HR, "EMP-5"), (HR, "EMP-1"), (HR, "EMP-999")):
        with pytest.raises(AttendanceError, match="Employee not found") as error:
            service.get_person_month(viewer, hidden, None)
        assert error.value.status_code == 404
    with pytest.raises(AttendanceError):
        service.get_person_month(EMPLOYEE, "EMP-4", None)


# ------------------------------------------------------------------ the month calendar
def test_the_month_calendar_and_summary(database):
    database.leaves.append({"employee_id": 3, "leave_type": "casual", "start_date": "2026-11-04", "end_date": "2026-11-04", "day_type": "full"})
    database.holidays["2026-11-06"] = "Founders Day"
    work_day(database, "10:00", "19:00", "2026-11-02")                      # present
    work_day(database, "10:00", "14:40", "2026-11-03")                      # half day
    work_day(database, "10:00", None, "2026-11-09")                         # never checked out
    work_day(database, "10:50", "17:50", "2026-11-10")                      # late, present, early leave
    database.set_time("10:05")
    month = service.get_my_month(EMPLOYEE, "2026-11")
    by_date = {day["date"]: day for day in month["days"]}
    assert len(month["days"]) == 12                                         # 1 to 12 November: nothing after today
    assert [by_date[day]["status"] for day in ("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")] == \
        ["weekend", "present", "half_day", "on_leave", "absent", "holiday"]
    assert by_date["2026-11-04"]["note"] == "Casual Leave (full day)" and by_date["2026-11-06"]["note"] == "Founders Day"
    assert by_date["2026-11-09"]["status"] == "incomplete"
    assert by_date["2026-11-10"]["is_early_leave"] is True and by_date["2026-11-10"]["is_late"] is True
    assert by_date["2026-11-11"]["status"] == "absent" and by_date["2026-11-12"]["status"] == "not_checked_in"
    summary = month["summary"]
    assert (summary["present"], summary["half_day"], summary["on_leave"], summary["incomplete"], summary["late_days"], summary["early_leave_days"]) == (2, 1, 1, 1, 1, 1)
    assert summary["worked_minutes"] == 540 + 280 + 420


def test_a_future_or_unreadable_month_is_refused(database):
    with pytest.raises(AttendanceError, match="not started"):
        service.get_my_month(EMPLOYEE, "2026-12")
    with pytest.raises(AttendanceError, match="read the month"):
        service.get_my_month(EMPLOYEE, "2026-13")


# ------------------------------------------------------------------ missed check-in / check-out
def ask(person, day="2026-11-10", check_in=None, check_out="19:00", reason="Forgot to check out"):
    return service.create_correction(person, date.fromisoformat(day), check_in, check_out, reason)


def test_a_missed_check_out_is_fixed_after_the_pm_approves(database):
    work_day(database, "10:00", None, "2026-11-10")
    database.set_time("10:05")
    request = ask(EMPLOYEE)
    assert request["status"] == "pending" and request["full_name"] == "Ann Employee"
    assert service.get_my_month(EMPLOYEE, "2026-11")["days"][9]["status"] == "incomplete"
    decided = service.decide_correction(PM, request["id"], "approved", "ok")
    assert (decided["status"], decided["decision_note"], decided["decided_by"]) == ("approved", "ok", 2)
    row = database.get_attendance(3, "2026-11-10")
    assert (row["status"], row["worked_minutes"], row["source"]) == ("present", 540, "correction")


def test_a_missed_check_in_day_with_no_row_is_created(database):
    request = ask(EMPLOYEE, day="2026-11-11", check_in="10:10", check_out="19:20")
    service.decide_correction(HR, request["id"], "approved")
    row = database.get_attendance(3, "2026-11-11")
    assert (row["status"], row["is_late"], row["source"]) == ("present", False, "correction")


def test_hr_can_also_decide_an_employees_request_and_reject_leaves_the_row_alone(database):
    work_day(database, "10:00", None, "2026-11-10")
    request = ask(EMPLOYEE)
    decided = service.decide_correction(HR, request["id"], "rejected", "Not true")
    assert decided["status"] == "rejected"
    assert database.get_attendance(3, "2026-11-10")["check_out_at"] is None


def test_a_rejection_needs_a_comment(database):
    work_day(database, "10:00", None, "2026-11-10")
    request = ask(EMPLOYEE)
    for empty_note in (None, "", "   "):
        with pytest.raises(AttendanceError, match="reason for rejecting") as error:
            service.decide_correction(PM, request["id"], "rejected", empty_note)
        assert error.value.status_code == 400
    assert database.get_correction(request["id"])["status"] == "pending"
    assert service.decide_correction(PM, request["id"], "rejected", "No proof")["decision_note"] == "No proof"


def test_the_pms_request_goes_to_hr_and_hr_requests_are_automatic(database):
    work_day(database, "10:00", None, "2026-11-10", employee=PM)
    request = ask(PM)
    with pytest.raises(AttendanceError, match="could not find") as error:
        service.decide_correction(PM, request["id"], "approved")                 # nobody approves their own
    assert error.value.status_code == 404
    with pytest.raises(AttendanceError):
        service.decide_correction(EMPLOYEE, request["id"], "approved")
    assert service.decide_correction(HR, request["id"], "approved")["status"] == "approved"

    work_day(database, "10:00", None, "2026-11-11", employee=HR)
    automatic = ask(HR, day="2026-11-11")
    assert (automatic["status"], automatic["decision_note"], automatic["decided_by"]) == ("approved", "Auto", 1)
    assert database.get_attendance(1, "2026-11-11")["status"] == "present"


def test_hr_cannot_decide_another_hr_or_hidden_people(database):
    work_day(database, "10:00", None, "2026-11-10", employee=OTHER_HR)
    request = ask(OTHER_HR)                                                      # automatic, so it is not even waiting
    assert request["decision_note"] == "Auto"
    assert service.list_correction_inbox(HR) == []


def test_only_the_last_7_working_days_can_be_corrected(database):
    # Today is Thursday 12 November. Weekends are not counted, so 7 working days back is Tuesday 3 November.
    work_day(database, "10:00", None, "2026-11-03")
    database.set_time("10:05")                                                   # back to today
    assert ask(EMPLOYEE, day="2026-11-03")["status"] == "pending"
    with pytest.raises(AttendanceError, match="within 7 working days"):
        ask(EMPLOYEE, day="2026-11-02", check_in="10:00")
    with pytest.raises(AttendanceError, match="not come yet"):
        ask(EMPLOYEE, day="2026-11-13", check_in="10:00")


def test_a_weekend_cannot_be_corrected(database):
    for weekend_day in ("2026-11-07", "2026-11-08"):                              # Saturday and Sunday
        with pytest.raises(AttendanceError, match="week offs"):
            ask(EMPLOYEE, day=weekend_day, check_in="10:00")


def test_working_days_are_counted_without_weekends():
    from datetime import date as day_of
    assert service.count_working_days_after(day_of(2026, 11, 6), day_of(2026, 11, 9)) == 1     # Friday to Monday
    assert service.count_working_days_after(day_of(2026, 11, 7), day_of(2026, 11, 8)) == 0     # a weekend
    assert service.count_working_days_after(day_of(2026, 11, 2), day_of(2026, 11, 12)) == 8


def test_a_correction_must_make_sense(database):
    with pytest.raises(AttendanceError, match="check-in time, the check-out time, or both"):
        ask(EMPLOYEE, check_in=None, check_out=None)
    with pytest.raises(AttendanceError, match="read the check-out time"):
        ask(EMPLOYEE, check_out="7pm")
    with pytest.raises(AttendanceError, match="no check-in on that day"):
        ask(EMPLOYEE, check_out="19:00")                                          # no row and no check-in given
    work_day(database, "10:00", None, "2026-11-10")
    with pytest.raises(AttendanceError, match="after the check-in"):
        ask(EMPLOYEE, check_out="09:00")
    work_day(database, "10:00", "19:00", "2026-11-11")
    with pytest.raises(AttendanceError, match="nothing to correct"):
        ask(EMPLOYEE, day="2026-11-11", check_in="10:00", check_out="19:00")


def test_one_waiting_request_per_day_and_one_decision_each(database):
    work_day(database, "10:00", None, "2026-11-10")
    request = ask(EMPLOYEE)
    with pytest.raises(AttendanceError, match="already have a waiting") as error:
        ask(EMPLOYEE, check_out="18:00")
    assert error.value.status_code == 409
    service.decide_correction(PM, request["id"], "approved")
    with pytest.raises(AttendanceError, match="already approved") as error:
        service.decide_correction(HR, request["id"], "rejected")
    assert error.value.status_code == 409


def test_the_inbox_shows_only_requests_of_the_people_below(database):
    work_day(database, "10:00", None, "2026-11-10")
    work_day(database, "10:00", None, "2026-11-10", employee=PM)
    employee_request, pm_request = ask(EMPLOYEE), ask(PM)
    assert [row["id"] for row in service.list_correction_inbox(PM)] == [employee_request["id"]]
    assert sorted(row["id"] for row in service.list_correction_inbox(HR)) == sorted([employee_request["id"], pm_request["id"]])
    assert service.list_correction_inbox(HR)[0]["current_check_in_at"] is not None
    with pytest.raises(AttendanceError):
        service.list_correction_inbox(EMPLOYEE)
    assert [row["id"] for row in service.list_my_corrections(EMPLOYEE)] == [employee_request["id"]]


def test_a_failed_approval_puts_the_request_back(database, monkeypatch):
    work_day(database, "10:00", None, "2026-11-10")
    request = ask(EMPLOYEE)
    monkeypatch.setattr(service, "apply_correction", lambda row, settings: (_ for _ in ()).throw(RuntimeError("database down")))
    with pytest.raises(RuntimeError):
        service.decide_correction(PM, request["id"], "approved")
    assert database.get_correction(request["id"])["status"] == "pending"


# ------------------------------------------------------------------ the chat text
def test_chat_text_for_myself_and_for_people_below(database):
    work_day(database, "10:00", "19:00", "2026-11-10")
    own_text = service.build_chat_text(EMPLOYEE, "", None)
    assert "Attendance for 2026-11" in own_text and "2026-11-10 Tue: present, in 10:00, out 19:00" in own_text
    assert "These records belong to" not in own_text
    other_text = service.build_chat_text(HR, "EMP-3", "2026-11")
    assert other_text.startswith("These records belong to Ann Employee (EMP-3)")
    assert "Attendance for 2026-11" in service.build_chat_text(PM, "Sal Sales", None)


def test_chat_never_shows_hidden_people(database):
    no_answer = "I don't have an answer for this question."
    for viewer, person in ((EMPLOYEE, "EMP-4"), (EMPLOYEE, "Pam PM"), (HR, "CEO"), (HR, "the CTO"), (HR, "EMP-5"), (HR, "EMP-6"),
                           (HR, "EMP-7"), (PM, "EMP-1"), (PM, "EMP-6")):
        with pytest.raises(AttendanceError) as error:
            service.build_chat_text(viewer, person, None)
        assert error.value.message == no_answer
    with pytest.raises(AttendanceError, match="exactly one employee"):
        service.build_chat_text(HR, "Nobody Here", None)
