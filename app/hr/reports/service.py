"""
hr/reports/service.py - the twice-daily WhatsApp HR report (attendance + leave + WFH for the day),
and the list of numbers it goes to.

Rules:
    - Only the super admin manages the number list (add/remove), capped at MAXIMUM_NUMBERS.
    - The report is built for one day (today, India time, unless a day is given) from the same
      attendance/leave data the team attendance page already computes - nothing new is worked out
      here that the rest of the app doesn't already know how to work out.
    - Sending goes through AiSensy (see aisensy.py next to this file). One number AiSensy refuses
      does not stop the others - send_daily_report() tries every number and reports what happened
      to each.
"""
import logging

from app.hr.attendance import service as attendance_service
from app.hr.attendance import store as attendance_store
from app.hr.leave import service as leave_service
from app.hr.reports import aisensy
from app.hr.reports import store as reports_store

logger = logging.getLogger(__name__)

MAXIMUM_NUMBERS = 10
MINIMUM_PHONE_DIGITS = 10
MAXIMUM_PHONE_DIGITS = 15


class ReportError(Exception):
    """A message for the super admin (for example: too many numbers already)."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def require_superadmin(admin):
    if admin.get("role") != "superadmin":
        raise ReportError("Only the super admin can do this.", 403)


def normalize_phone_number(phone_number):
    """Strip spaces/dashes and keep a leading "+". Needs 10-15 digits, like a WhatsApp number
    with its country code (for example +919876543210)."""
    cleaned = "".join(character for character in (phone_number or "").strip() if character.isdigit() or character == "+")
    digits = cleaned.lstrip("+")
    if not digits.isdigit() or not (MINIMUM_PHONE_DIGITS <= len(digits) <= MAXIMUM_PHONE_DIGITS):
        raise ReportError("That does not look like a WhatsApp number. Include the country code, for example +919876543210.")
    return f"+{digits}"


# ------------------------------------------------------------------ managing the number list
def list_numbers(admin):
    require_superadmin(admin)
    return reports_store.list_numbers()


def add_number(admin, label, phone_number):
    require_superadmin(admin)
    label = (label or "").strip()
    if not label:
        raise ReportError("Please give this number a label (whose number it is).")
    phone_number = normalize_phone_number(phone_number)
    if len(reports_store.list_numbers()) >= MAXIMUM_NUMBERS:
        raise ReportError(f"There are already {MAXIMUM_NUMBERS} numbers. Remove one before adding another.")
    saved = reports_store.insert_number(label, phone_number)
    if saved is None:
        raise ReportError("That number is already on the list.", 409)
    return saved


def remove_number(admin, number_id):
    require_superadmin(admin)
    removed = reports_store.delete_number(number_id)
    if removed is None:
        raise ReportError("I could not find that number.", 404)
    return removed


# ------------------------------------------------------------------ building the report
def people_with_entries(for_date):
    """Everyone with attendance access, each paired with their day entry for one date (the same
    shape build_day_entry gives the team attendance page: status, is_late, note, ...). The super
    admin has no attendance of its own (get_access_level still returns a level for it, so it has
    to be excluded explicitly here - the same thing superadmin/service.py's list_employees does)."""
    people = [person for person in attendance_store.list_people()
             if person.get("role") != "superadmin" and attendance_service.get_access_level(person) is not None]
    employee_ids = [person["id"] for person in people]
    data = attendance_service.load_calendar_data(employee_ids, for_date, for_date)
    return [(person, attendance_service.build_entries_for_person(person["id"], for_date, for_date, data, for_date)[0])
            for person in people]


def day_off_note(for_date):
    """Why nobody is expected in today, company-wide - a weekend or a company holiday - or None
    on an ordinary working day."""
    if for_date.weekday() >= 5:
        return "Today is a weekend."
    holidays = attendance_store.list_holidays(for_date.isoformat(), for_date.isoformat())
    if holidays:
        return f"Today is a company holiday ({holidays[0]['name']})."
    return None


# The AiSensy template has 7 separate body variables ({{1}} through {{7}}), in this exact order -
# NOT one combined block of text. Each value must be a single line (WhatsApp templates don't
# render embedded newlines well), and an empty list becomes the word "None" rather than being left
# blank, since AiSensy/WhatsApp refuses an empty template variable.
REPORT_FIELD_NAMES = ("report_date", "number_tracked", "number_present", "late_names", "absent_names", "on_leave", "work_from_home")


def build_daily_report_fields(for_date=None):
    """The report as 7 separate single-line values, in the order the AiSensy template expects
    them (REPORT_FIELD_NAMES / see areas/hrms-whatsapp-report.md): report date, number tracked,
    number present, late names, not-checked-in/absent names, on-leave names, and WFH names."""
    for_date = for_date or attendance_service.get_today()
    pairs = people_with_entries(for_date)

    present = [person for person, entry in pairs if entry["status"] in ("present", "half_day", "checked_in")]
    late = [person for person, entry in pairs if entry["is_late"]]
    absent = [person for person, entry in pairs if entry["status"] in ("absent", "not_checked_in")]
    on_leave = [(person, entry) for person, entry in pairs if entry["status"] == "on_leave"]
    wfh = [person for person, entry in pairs if entry["status"] == "wfh"]

    report_date = leave_service.format_date(for_date)
    note = day_off_note(for_date)
    if note:
        report_date = f"{report_date} ({note})"

    def names_or_none(people):
        return ", ".join(person["full_name"] for person in people) or "None"

    on_leave_text = ", ".join(f"{person['full_name']} ({entry['note']})" for person, entry in on_leave) or "None"

    return [
        report_date,
        str(len(pairs)),
        str(len(present)),
        names_or_none(late),
        names_or_none(absent),
        on_leave_text,
        names_or_none(wfh),
    ]


# ------------------------------------------------------------------ sending
def send_daily_report(for_date=None):
    """Build today's report and send it to every configured number. One number's failure (for
    example AiSensy refusing it) never stops the others. Returns one result per number."""
    report_fields = build_daily_report_fields(for_date)
    results = []
    for entry in reports_store.list_numbers():
        try:
            aisensy.send_report(entry["phone_number"], report_fields)
            results.append({"label": entry["label"], "phone_number": entry["phone_number"], "ok": True, "error": None})
        except aisensy.AiSensyError as error:
            logger.error("WhatsApp HR report to %s (%s) failed: %s", entry["label"], entry["phone_number"], error)
            results.append({"label": entry["label"], "phone_number": entry["phone_number"], "ok": False, "error": str(error)})
    return results


def send_daily_report_now(admin, for_date=None):
    """The super admin's own "send now" button, mainly to test the AiSensy setup without waiting
    for 11am or 5pm."""
    require_superadmin(admin)
    return send_daily_report(for_date)
