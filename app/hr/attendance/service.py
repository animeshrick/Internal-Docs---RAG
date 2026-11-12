"""
hr/attendance/service.py - the attendance RULES. No web code and no AI here, so every rule can be tested on its own.

Hierarchy (top to low): CEO, CTO, HR, PM, then everybody else (employee, sales, finance ...).
    - The CEO and the CTO have no attendance at all (no check-in, no menu, nobody sees them).
      The CEO is the login role ceo; the CTO is the employee with is_confidential = true.
    - Everybody else checks in and out for themselves.
    - Each level sees the levels BELOW it: HR sees the PM and all employees, the PM sees all employees,
      an employee sees only themself. Nobody sees upward, and HR does not see another HR.

The day (all times are India time; the numbers live in the attendance_settings table):
    - Check-in after late_after (10:45, that is the 45 minute buffer after the 10:00 start) is marked late.
    - Worked time less than half_day_hours (4.5) is absent.
    - A check-out at or before early_leave_after (15:00) is a half day.
    - A check-out after 15:00 but before the end of the office day (19:00) is present with an early-leave mark.
    - A check-out at or after 19:00 is present.

Missed check-in or check-out:
    - Asked for a day that is at most correction_window_days (7) old. Older days cannot be requested.
    - Employee requests go to the PM or HR, the PM's request goes to HR, and HR's own request is approved
      automatically with the remark "Auto".
Check-in and check-out work only from the office network when network_check_enabled is true (off while testing).
"""
import ipaddress
from datetime import date, datetime, time, timedelta, timezone

from app.hr.attendance import store as attendance_store
from app.hr.leave import service as leave_service
from app.hr.leave import store as leave_store
from app.hr.leave.service import COMPANY_TIME_ZONE
from app.hr.people import service as people_service
from app.hr.people import store as people_store
from app.identity.auth import parse_timestamp

LEVEL_HR = "hr"
LEVEL_PM = "pm"
LEVEL_EMPLOYEE = "employee"
LEVEL_RANK = {LEVEL_HR: 1, LEVEL_PM: 2, LEVEL_EMPLOYEE: 3}      # a smaller number is higher in the hierarchy

AUTO_APPROVAL_NOTE = "Auto"
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class AttendanceError(Exception):
    """A problem to show to the employee, with the HTTP status code to use on the website API."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_now():
    """The current moment (UTC). A separate function so tests can control the clock."""
    return leave_service.get_now()


def get_today():
    return get_now().astimezone(COMPANY_TIME_ZONE).date()


# ------------------------------------------------------------------ who may do what
def normalise_person(person):
    """A token dictionary has "employee_id"; a database row has "id". Both get "employee_id".
    The person's own database id is never confused with another key this way."""
    return dict(person, employee_id=person.get("employee_id", person.get("id")))


def get_access_level(person):
    """'hr', 'pm' or 'employee'. None for the CEO and the CTO (no attendance for them)."""
    if person.get("role") == "ceo" or person.get("is_confidential"):
        return None
    if person.get("role") == "hr":
        return LEVEL_HR
    if leave_service.is_pm(person):
        return LEVEL_PM
    return LEVEL_EMPLOYEE


def require_attendance(person):
    """Stops the CEO and the CTO. Returns the person's level."""
    level = get_access_level(person)
    if level is None:
        raise AttendanceError("Attendance is not available for your account.", 403)
    return level


def require_viewer(viewer):
    """Only HR and the PM may look at other people."""
    level = require_attendance(viewer)
    if level == LEVEL_EMPLOYEE:
        raise AttendanceError("Only HR and the PM can see other people's attendance.", 403)
    return level


def can_view(viewer, target):
    """True when the viewer is the person themself, or is strictly higher in the hierarchy than the target."""
    viewer, target = normalise_person(viewer), normalise_person(target)
    viewer_level, target_level = get_access_level(viewer), get_access_level(target)
    if viewer_level is None or target_level is None:
        return False
    if viewer["employee_id"] == target["employee_id"]:
        return True
    return LEVEL_RANK[viewer_level] < LEVEL_RANK[target_level]


def list_visible_people(viewer):
    """The people (not the viewer) whose attendance this viewer may see."""
    return [normalise_person(person) for person in attendance_store.list_people()
            if normalise_person(person)["employee_id"] != viewer["employee_id"] and can_view(viewer, person)]


# ------------------------------------------------------------------ settings, time and the office network
def parse_clock(value):
    """'10:45:00' or '10:45' (as the database sends a time column) -> a time."""
    return time.fromisoformat(str(value))


def read_settings():
    row = attendance_store.get_settings()
    return {
        "office_start": parse_clock(row["office_start"]),
        "office_end": parse_clock(row["office_end"]),
        "late_after": parse_clock(row["late_after"]),
        "early_leave_after": parse_clock(row["early_leave_after"]),
        "half_day_minutes": int(round(float(row["half_day_hours"]) * 60)),
        "network_check_enabled": bool(row["network_check_enabled"]),
        "correction_window_days": int(row["correction_window_days"]),
    }


def to_local(moment_text):
    """A timestamp from the database (UTC text) -> a datetime in India time."""
    return parse_timestamp(moment_text).astimezone(COMPANY_TIME_ZONE)


def format_clock(moment_text):
    """'10:05 AM' in India time."""
    return to_local(moment_text).strftime("%I:%M %p").lstrip("0")


def check_network(settings, client_ip):
    """When the network check is on, only an address inside an active office network may pass."""
    if not settings["network_check_enabled"]:
        return
    try:
        address = ipaddress.ip_address(str(client_ip))
    except ValueError:
        raise AttendanceError("Check-in and check-out are possible only from the office network.", 403)
    for network in attendance_store.list_active_office_networks():
        if address in ipaddress.ip_network(network["cidr"], strict=False):
            return
    raise AttendanceError("Check-in and check-out are possible only from the office network.", 403)


# ------------------------------------------------------------------ what a day counts as
def evaluate_day(check_in_at, check_out_at, settings):
    """
    check_in_at and check_out_at are timezone-aware datetimes (check_out_at may be None).
    Returns the status and marks of the day.
    """
    is_late = check_in_at.astimezone(COMPANY_TIME_ZONE).time() > settings["late_after"]
    if check_out_at is None:
        return {"status": "checked_in", "is_late": is_late, "is_early_leave": False, "worked_minutes": None}

    worked_minutes = int((check_out_at - check_in_at).total_seconds() // 60)
    check_out_clock = check_out_at.astimezone(COMPANY_TIME_ZONE).time()
    is_early_leave = False
    if worked_minutes < settings["half_day_minutes"]:
        status = "absent"
    elif check_out_clock <= settings["early_leave_after"]:
        status = "half_day"
    else:
        status = "present"
        is_early_leave = check_out_clock < settings["office_end"]
    return {"status": status, "is_late": is_late, "is_early_leave": is_early_leave, "worked_minutes": worked_minutes}


def find_non_working_reason(day, holiday_name, leave_row):
    """Why nothing is expected on this day (weekend, holiday, full day of approved leave), or None."""
    if day.weekday() >= 5:
        return "weekend"
    if holiday_name:
        return "holiday"
    if leave_row is not None and leave_row["day_type"] == "full":
        return "on_leave"
    return None


def build_day_entry(day, row, holiday_name, leave_row, leave_types, today):
    """One line of a calendar: the day, its status and the times. row is the attendance row (or None)."""
    entry = {"date": day.isoformat(), "weekday": WEEKDAY_NAMES[day.weekday()], "status": None, "check_in_at": None,
             "check_out_at": None, "worked_minutes": None, "is_late": False, "is_early_leave": False, "note": None}
    if leave_row is not None:
        leave_name = leave_types.get(leave_row["leave_type"], {}).get("name", leave_row["leave_type"])
        entry["note"] = f"{leave_name} ({'half day' if leave_row['day_type'] == 'half' else 'full day'})"
    elif holiday_name:
        entry["note"] = holiday_name

    if row is not None:
        status = row["status"]
        if status == "checked_in" and day < today:
            status = "incomplete"                      # never checked out: needs a correction
        entry.update(status=status, check_in_at=row["check_in_at"], check_out_at=row["check_out_at"],
                     worked_minutes=row["worked_minutes"], is_late=row["is_late"], is_early_leave=row["is_early_leave"])
    elif day.weekday() >= 5:
        entry["status"] = "weekend"
    elif holiday_name:
        entry["status"] = "holiday"
    elif leave_row is not None:
        entry["status"] = "on_leave"
    elif day == today:
        entry["status"] = "not_checked_in"
    else:
        entry["status"] = "absent"
    return entry


def build_summary(entries):
    """Counts for a list of day entries."""
    counts = {status: 0 for status in ("present", "half_day", "absent", "incomplete", "on_leave", "holiday", "weekend", "checked_in", "not_checked_in")}
    for entry in entries:
        counts[entry["status"]] += 1
    counts["late_days"] = sum(1 for entry in entries if entry["is_late"])
    counts["early_leave_days"] = sum(1 for entry in entries if entry["is_early_leave"])
    counts["worked_minutes"] = sum(entry["worked_minutes"] or 0 for entry in entries)
    return counts


def load_calendar_data(employee_ids, first_day, last_day):
    """Everything needed to build day entries for these people: rows, holidays, approved leave and leave type names."""
    rows = {(row["employee_id"], row["work_date"]): row for row in attendance_store.list_attendance(employee_ids, first_day, last_day)}
    holidays = {row["holiday_date"]: row["name"] for row in attendance_store.list_holidays(first_day, last_day)}
    leaves = attendance_store.list_approved_leaves(employee_ids, first_day, last_day)
    return rows, holidays, leaves, leave_store.get_leave_types()


def find_leave(leaves, employee_id, day):
    for leave_row in leaves:
        if leave_row["employee_id"] == employee_id and date.fromisoformat(leave_row["start_date"]) <= day <= date.fromisoformat(leave_row["end_date"]):
            return leave_row
    return None


def build_entries_for_person(employee_id, first_day, last_day, data, today):
    rows, holidays, leaves, leave_types = data
    entries = []
    day = first_day
    while day <= last_day:
        entries.append(build_day_entry(day, rows.get((employee_id, day.isoformat())), holidays.get(day.isoformat()),
                                       find_leave(leaves, employee_id, day), leave_types, today))
        day += timedelta(days=1)
    return entries


# ------------------------------------------------------------------ check in and check out
def describe_today(employee_id, today):
    """Returns (row, why nothing is expected today or None, holiday name)."""
    row = attendance_store.get_attendance(employee_id, today)
    holidays = {item["holiday_date"]: item["name"] for item in attendance_store.list_holidays(today, today)}
    leave_row = find_leave(attendance_store.list_approved_leaves([employee_id], today, today), employee_id, today)
    reason = find_non_working_reason(today, holidays.get(today.isoformat()), leave_row)
    return row, reason, holidays.get(today.isoformat())


def explain_non_working_day(reason, holiday_name):
    return {"weekend": "Today is a weekend.", "holiday": f"Today is a company holiday ({holiday_name}).",
            "on_leave": "You are on approved leave today."}.get(reason)


def get_today_state(employee):
    require_attendance(employee)
    settings = read_settings()
    today = get_today()
    row, reason, holiday_name = describe_today(employee["employee_id"], today)
    evaluation = row or {}
    return {
        "work_date": today.isoformat(),
        "day_note": explain_non_working_day(reason, holiday_name),
        "checked_in_at": evaluation.get("check_in_at"),
        "checked_out_at": evaluation.get("check_out_at"),
        "status": evaluation.get("status", "not_checked_in"),
        "is_late": evaluation.get("is_late", False),
        "is_early_leave": evaluation.get("is_early_leave", False),
        "worked_minutes": evaluation.get("worked_minutes"),
        "can_check_in": row is None and reason is None,
        "can_check_out": row is not None and row["check_out_at"] is None,
        "office_start": settings["office_start"].strftime("%H:%M"),
        "office_end": settings["office_end"].strftime("%H:%M"),
        "late_after": settings["late_after"].strftime("%H:%M"),
        "network_check_enabled": settings["network_check_enabled"],
    }


def check_in(employee, client_ip):
    require_attendance(employee)
    settings = read_settings()
    check_network(settings, client_ip)
    now = get_now()
    today = now.astimezone(COMPANY_TIME_ZONE).date()

    row, reason, holiday_name = describe_today(employee["employee_id"], today)
    if row is not None:
        raise AttendanceError(f"You already checked in today at {format_clock(row['check_in_at'])}.", 409)
    if reason is not None:
        raise AttendanceError(f"{explain_non_working_day(reason, holiday_name)} No check-in is needed.", 400)

    evaluation = evaluate_day(now, None, settings)
    try:
        attendance_store.insert_attendance({
            "employee_id": employee["employee_id"], "work_date": today.isoformat(), "check_in_at": now.isoformat(),
            "check_in_ip": str(client_ip), "status": evaluation["status"], "is_late": evaluation["is_late"], "source": "self"})
    except attendance_store.AttendanceConflictError:
        raise AttendanceError("You already checked in today.", 409)
    return get_today_state(employee)


def check_out(employee, client_ip):
    require_attendance(employee)
    settings = read_settings()
    check_network(settings, client_ip)
    now = get_now()
    today = now.astimezone(COMPANY_TIME_ZONE).date()

    row = attendance_store.get_attendance(employee["employee_id"], today)
    if row is None:
        raise AttendanceError("You have not checked in today. If you forgot, ask for a correction.", 400)
    if row["check_out_at"] is not None:
        raise AttendanceError(f"You already checked out today at {format_clock(row['check_out_at'])}.", 409)

    evaluation = evaluate_day(parse_timestamp(row["check_in_at"]), now, settings)
    attendance_store.update_attendance(row["id"], {"check_out_at": now.isoformat(), "check_out_ip": str(client_ip), **evaluation})
    return get_today_state(employee)


# ------------------------------------------------------------------ calendars (mine, one person, a whole day of the team)
def parse_month(month_text):
    """'2026-11' (or nothing = this month) -> (first day, last day up to today). A future month is refused."""
    today = get_today()
    try:
        first_day = date.fromisoformat(f"{month_text}-01") if month_text else today.replace(day=1)
    except ValueError:
        raise AttendanceError("I could not read the month. Please give it like 2026-11.")
    if first_day > today:
        raise AttendanceError("That month has not started yet.")
    next_month_start = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_day, min(next_month_start - timedelta(days=1), today)


def get_month_view(person, month_text):
    """The calendar and summary of one person (the caller already checked that the viewer may see them)."""
    first_day, last_day = parse_month(month_text)
    data = load_calendar_data([person["employee_id"]], first_day, last_day)
    entries = build_entries_for_person(person["employee_id"], first_day, last_day, data, get_today())
    return {"emp_id": person["emp_id"], "full_name": person["full_name"], "month": first_day.strftime("%Y-%m"),
            "days": entries, "summary": build_summary(entries)}


def get_my_month(employee, month_text):
    require_attendance(employee)
    return get_month_view(employee, month_text)


def get_person_month(viewer, emp_id_text, month_text):
    """HR and the PM open one person's month. An unknown or hidden person gives the same answer."""
    require_viewer(viewer)
    found = people_store.find_employee(emp_id_text)
    person = normalise_person(found) if found else None
    if person is None or person["employee_id"] == viewer["employee_id"] or not can_view(viewer, person):
        raise AttendanceError("Employee not found.", 404)
    return get_month_view(person, month_text)


def get_team_day(viewer, day_text):
    """One day of everybody the viewer may see."""
    require_viewer(viewer)
    today = get_today()
    try:
        day = date.fromisoformat(day_text) if day_text else today
    except ValueError:
        raise AttendanceError("I could not read the date. Please give it like 2026-11-12.")
    if day > today:
        raise AttendanceError("That day has not come yet.")

    people = list_visible_people(viewer)
    data = load_calendar_data([person["employee_id"] for person in people], day, day)
    entries = []
    for person in people:
        entry = build_entries_for_person(person["employee_id"], day, day, data, today)[0]
        entries.append(dict(entry, emp_id=person["emp_id"], full_name=person["full_name"], dept=person["dept"]))
    return {"date": day.isoformat(), "people": entries, "summary": build_summary(entries)}


# ------------------------------------------------------------------ missed check-in or check-out (correction requests)
def parse_clock_text(text, label):
    if text is None or str(text).strip() == "":
        return None
    try:
        return time.fromisoformat(str(text).strip())
    except ValueError:
        raise AttendanceError(f"I could not read the {label}. Please give it like 18:30.")


def make_moment(work_date, clock):
    """A clock time on a day in India time -> a UTC datetime."""
    return datetime.combine(work_date, clock, tzinfo=COMPANY_TIME_ZONE).astimezone(timezone.utc)


def merge_times(existing_row, requested_in, requested_out):
    """The check-in and check-out the day would have after the correction (datetimes, or None)."""
    final_in = requested_in or (parse_timestamp(existing_row["check_in_at"]) if existing_row else None)
    final_out = requested_out or (parse_timestamp(existing_row["check_out_at"]) if existing_row and existing_row["check_out_at"] else None)
    if final_in is None:
        raise AttendanceError("There is no check-in on that day, so please give the check-in time too.")
    if final_out is not None and final_out <= final_in:
        raise AttendanceError("The check-out must be after the check-in.")
    return final_in, final_out


def apply_correction(correction_row, settings):
    """Write the corrected times of an approved request into the daily record."""
    employee_id, work_date = correction_row["employee_id"], correction_row["work_date"]
    requested_in = parse_timestamp(correction_row["requested_check_in"]) if correction_row["requested_check_in"] else None
    requested_out = parse_timestamp(correction_row["requested_check_out"]) if correction_row["requested_check_out"] else None
    existing_row = attendance_store.get_attendance(employee_id, work_date)
    final_in, final_out = merge_times(existing_row, requested_in, requested_out)
    evaluation = evaluate_day(final_in, final_out, settings)
    fields = {"check_in_at": final_in.isoformat(), "check_out_at": final_out.isoformat() if final_out else None,
              "source": "correction", **evaluation}
    if existing_row:
        attendance_store.update_attendance(existing_row["id"], fields)
    else:
        attendance_store.insert_attendance(dict(fields, employee_id=employee_id, work_date=work_date))


def finish_correction(correction_row, decided_by_id, decision, note, settings):
    """Mark the request decided (only if it is still pending) and, when approved, write the times. Returns the request."""
    updated_row = attendance_store.update_correction(
        correction_row["id"], {"status": decision, "decided_by": decided_by_id, "decided_at": get_now().isoformat(), "decision_note": note},
        expected_status="pending")
    if updated_row is None:
        raise AttendanceError("Someone else already decided this request.", 409)
    if decision == "approved":
        try:
            apply_correction(updated_row, settings)
        except Exception:
            # Put the request back so it can be decided again, then show the problem.
            attendance_store.update_correction(correction_row["id"], {"status": "pending", "decided_by": None, "decided_at": None,
                                                                        "decision_note": None}, expected_status=decision)
            raise
    return updated_row


def create_correction(employee, work_date, check_in_text, check_out_text, reason):
    level = require_attendance(employee)
    settings = read_settings()
    today = get_today()
    if work_date > today:
        raise AttendanceError("You cannot ask for a correction for a day that has not come yet.")
    if (today - work_date).days > settings["correction_window_days"]:
        raise AttendanceError(f"A correction can be asked for only within {settings['correction_window_days']} days. That day is older.")

    requested_in = parse_clock_text(check_in_text, "check-in time")
    requested_out = parse_clock_text(check_out_text, "check-out time")
    if requested_in is None and requested_out is None:
        raise AttendanceError("Please give the check-in time, the check-out time, or both.")
    requested_in_moment = make_moment(work_date, requested_in) if requested_in else None
    requested_out_moment = make_moment(work_date, requested_out) if requested_out else None

    existing_row = attendance_store.get_attendance(employee["employee_id"], work_date.isoformat())
    final_in, final_out = merge_times(existing_row, requested_in_moment, requested_out_moment)
    if existing_row and final_in == parse_timestamp(existing_row["check_in_at"]) and \
            final_out == (parse_timestamp(existing_row["check_out_at"]) if existing_row["check_out_at"] else None):
        raise AttendanceError("These times are the same as the saved attendance, so there is nothing to correct.")

    try:
        row = attendance_store.insert_correction({
            "employee_id": employee["employee_id"], "work_date": work_date.isoformat(), "reason": reason.strip(),
            "requested_check_in": requested_in_moment.isoformat() if requested_in_moment else None,
            "requested_check_out": requested_out_moment.isoformat() if requested_out_moment else None})
    except attendance_store.AttendanceConflictError:
        raise AttendanceError("You already have a waiting correction request for that day.", 409)

    if level == LEVEL_HR:
        # Nobody is above HR in this section, so HR's own request is approved at once.
        row = finish_correction(row, employee["employee_id"], "approved", AUTO_APPROVAL_NOTE, settings)
    return describe_correction(row, {employee["employee_id"]: employee})


def describe_correction(row, people_by_id, attendance_row=None):
    person = people_by_id.get(row["employee_id"], {})
    return dict(row, emp_id=person.get("emp_id"), full_name=person.get("full_name"), dept=person.get("dept"),
                current_check_in_at=attendance_row["check_in_at"] if attendance_row else None,
                current_check_out_at=attendance_row["check_out_at"] if attendance_row else None)


def list_my_corrections(employee):
    require_attendance(employee)
    rows = attendance_store.list_corrections([employee["employee_id"]])
    return [describe_correction(row, {employee["employee_id"]: employee}) for row in rows]


def list_correction_inbox(viewer):
    """Waiting requests of the people below the viewer (the PM sees employees, HR sees the PM and employees)."""
    require_viewer(viewer)
    people = {person["employee_id"]: person for person in list_visible_people(viewer)}
    rows = attendance_store.list_corrections(list(people), status="pending")
    return [describe_correction(row, people, attendance_store.get_attendance(row["employee_id"], row["work_date"])) for row in rows]


def decide_correction(viewer, correction_id, decision, note=None):
    require_viewer(viewer)
    row = attendance_store.get_correction(correction_id)
    requester = attendance_store.get_person(row["employee_id"]) if row else None
    # A hidden or unknown request gets the same answer, so the reply never confirms that it exists.
    if row is None or requester is None or not can_view(viewer, requester) or requester["id"] == viewer["employee_id"]:
        raise AttendanceError("I could not find that request.", 404)
    if row["status"] != "pending":
        raise AttendanceError(f"This request was already {row['status']}.", 409)
    decided_row = finish_correction(row, viewer["employee_id"], decision, (note or "").strip() or None, read_settings())
    return describe_correction(decided_row, {requester["id"]: requester})


# ------------------------------------------------------------------ the chat: plain text about one person's attendance
def describe_entry_line(entry):
    status_names = {"present": "present", "half_day": "half day", "absent": "absent", "incomplete": "check-out missing",
                    "on_leave": "on leave", "holiday": "holiday", "weekend": "weekend", "checked_in": "checked in, not out yet",
                    "not_checked_in": "not checked in yet"}
    parts = [status_names[entry["status"]]]
    if entry["check_in_at"]:
        parts.append(f"in {format_clock(entry['check_in_at'])}" + (f", out {format_clock(entry['check_out_at'])}" if entry["check_out_at"] else ""))
    if entry["is_late"]:
        parts.append("late")
    if entry["is_early_leave"]:
        parts.append("early leave")
    if entry["note"]:
        parts.append(entry["note"])
    return f"{entry['date']} {entry['weekday']}: " + ", ".join(parts)


def build_chat_text(viewer, person_text, month_text):
    """
    The attendance of the viewer (empty person_text) or, for HR and the PM, of someone below them. Plain text for the AI to phrase.
    Everyone who may not see the person gets the same short answer as for an unknown person.
    """
    require_attendance(viewer)
    if people_service.is_asking_about_self(viewer, person_text):
        person, owner_line = viewer, ""
    else:
        if get_access_level(viewer) == LEVEL_EMPLOYEE or people_service.PROTECTED_TITLES.search(person_text or ""):
            raise AttendanceError(people_service.NO_ANSWER, 403)
        found = people_store.find_employee(person_text)
        if found is None:
            raise AttendanceError(people_service.NOT_FOUND, 404)
        person = normalise_person(found)
        if not can_view(viewer, person):
            raise AttendanceError(people_service.NO_ANSWER, 403)
        owner_line = f"These records belong to {person['full_name']} ({person['emp_id']}), not to the person asking.\n"

    month_view = get_month_view(person, month_text)
    summary = month_view["summary"]
    header = (f"Attendance for {month_view['month']} (days up to today only). Present {summary['present']}, half day {summary['half_day']}, "
              f"absent {summary['absent']}, check-out missing {summary['incomplete']}, on leave {summary['on_leave']}, "
              f"late {summary['late_days']}, early leave {summary['early_leave_days']}, worked {summary['worked_minutes'] // 60} hours "
              f"{summary['worked_minutes'] % 60} minutes.")
    lines = [describe_entry_line(entry) for entry in month_view["days"] if entry["status"] not in ("weekend",)]
    return owner_line + header + "\n" + "\n".join(lines)
