"""
hr/superadmin/service.py - what the super admin can do for other people.

    - Apply leave for anyone. It is approved at once, on any past date, and still follows the balance rules.
    - Set the check-in and check-out times of anyone on any past day.
Weekends and company holidays are a barrier for both: nothing can be saved on those days.
"""
from app.hr.attendance import service as attendance_service
from app.hr.attendance import store as attendance_store
from app.hr.attendance.service import AttendanceError
from app.hr.leave import service as leave_service
from app.hr.leave import store as leave_store
from app.hr.leave.service import LeaveError

SUPERADMIN_NOTE = "Applied by super admin"


def require_superadmin(admin):
    if admin.get("role") != "superadmin":
        raise LeaveError("Only the super admin can do this.", 403)


def list_employees(admin):
    """Everyone active, for the pickers on the super admin page."""
    require_superadmin(admin)
    return [{"emp_id": person["emp_id"], "full_name": person["full_name"], "dept": person.get("dept"), "role": person.get("role") or "employee",
             "has_attendance": attendance_service.get_access_level(person) is not None}
            for person in attendance_store.list_people() if person.get("role") != "superadmin"]    # the super admin has no leave or attendance


def list_leave_types(admin):
    require_superadmin(admin)
    return [{"code": leave_type["code"], "name": leave_type["name"]} for leave_type in leave_store.get_leave_types().values()]


def find_person(emp_id_text):
    wanted = (emp_id_text or "").strip().lower()
    for person in attendance_store.list_people():
        if str(person["emp_id"]).lower() == wanted:
            return person
    return None


def check_working_day(day, error_class):
    """The barrier: a week off or a company holiday cannot be used."""
    if day.weekday() >= 5:
        raise error_class(f"{day.isoformat()} is a week off (Saturday or Sunday), so nothing can be saved on it.")
    holidays = attendance_store.list_holidays(day.isoformat(), day.isoformat())
    if holidays:
        raise error_class(f"{day.isoformat()} is a company holiday ({holidays[0]['name']}), so nothing can be saved on it.")


def get_leave_balances(admin, emp_id_text, on_date):
    """The leave balances of one person for the leave year of on_date, so the page can warn before saving."""
    require_superadmin(admin)
    person = find_person(emp_id_text)
    if person is None:
        raise LeaveError("I could not find an active employee with that ID.", 404)
    leave_service.refuse_superadmin(person)
    return leave_store.get_leave_balances(person["id"], on_date)


def apply_leave(admin, emp_id_text, leave_type_code, start_date, end_date, day_type, reason):
    """Apply and approve leave for one person. Past dates are allowed, and a supporting document is not needed."""
    require_superadmin(admin)
    person = find_person(emp_id_text)
    if person is None:
        raise LeaveError("I could not find an active employee with that ID.", 404)
    leave_service.refuse_superadmin(person)
    employee_row = leave_store.get_employee_row(person["id"])
    end_date = end_date or start_date
    check_working_day(start_date, LeaveError)
    check_working_day(end_date, LeaveError)
    details = leave_service.check_new_leave(employee_row, leave_type_code, start_date, end_date, day_type, applied_by_chat=False,
                                            skip_backdate_limit=True, require_document=False,
                                            person_label=person["full_name"])
    try:
        saved_row = leave_store.insert_leave_request({
            "employee_id": employee_row["id"], "leave_type": details["leave_type"], "start_date": details["start_date"],
            "end_date": details["end_date"], "day_type": details["day_type"], "days": details["days"], "reason": reason,
            "status": "approved", "approver_id": admin["employee_id"], "decided_at": leave_service.get_now().isoformat(),
            "decision_note": SUPERADMIN_NOTE,
        })
    except leave_store.LeaveOverlapError:
        raise LeaveError("That person already has a leave on these dates.", 409)
    return {"leave_id": saved_row["id"], "message": f"{details['leave_type_name']} for {person['full_name']} "
            f"({leave_service.format_days(details['days'])} days) was saved and approved."}


def mark_attendance(admin, emp_id_text, work_date, check_in_text, check_out_text):
    """Set the times of one person on one past (or current) day. Replaces the times already saved for that day."""
    require_superadmin(admin)
    person = find_person(emp_id_text)
    if person is None:
        raise AttendanceError("I could not find an active employee with that ID.", 404)
    if person.get("role") == "superadmin" or attendance_service.get_access_level(person) is None:
        raise AttendanceError(f"{person['full_name']} has no attendance.", 400)
    if work_date > attendance_service.get_today():
        raise AttendanceError("That day has not come yet.")
    check_working_day(work_date, AttendanceError)

    requested_in = attendance_service.parse_clock_text(check_in_text, "check-in time")
    requested_out = attendance_service.parse_clock_text(check_out_text, "check-out time")
    if requested_in is None and requested_out is None:
        raise AttendanceError("Please give the check-in time, the check-out time, or both.")
    in_moment = attendance_service.make_moment(work_date, requested_in) if requested_in else None
    out_moment = attendance_service.make_moment(work_date, requested_out) if requested_out else None
    saved = attendance_service.write_day(person["id"], work_date.isoformat(), in_moment, out_moment, "superadmin", attendance_service.read_settings())
    return {"message": f"Attendance of {person['full_name']} on {work_date.isoformat()} was saved.", "status": saved["status"],
            "is_late": saved["is_late"], "is_early_leave": saved["is_early_leave"], "worked_minutes": saved["worked_minutes"]}
