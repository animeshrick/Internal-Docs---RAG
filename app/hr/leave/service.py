"""
hr/leave/service.py - the leave RULES. No web code and no AI here, so every rule can be tested on its own.

Rules (agreed with HR):
    - Casual 14 and sick 3 days per year, maternity 90 days per request (the numbers live in the leave_types table).
      Casual accrues: 2 on 1 January, then +1 on the 1st of each month. Sick is credited in full on 1 January.
      The leave year is the calendar year, and unused balance carries forward on 31 December.
    - Monday to Friday is the working week; weekends and company holidays are not counted.
    - Full day or half day. A half day is a single date.
    - One request spans at most 7 consecutive days (maternity has no such limit).
    - A leave can start at most 7 days in the past.
    - Maternity (Female only) is applied on the website with a document.
    - Sick Leave for more than 2 days needs a supporting document too (require_document_above_days in leave_types);
      2 days or less can still be applied in the chat with no document, as before.
    - Work From Home (wfh) has no yearly quota and no per-request day limit (limit_type "unlimited" -
      see 17_wfh_unlimited.sql).
    - EVERY leave - casual, sick, maternity and WFH alike - is decided only by the super admin. HR has
      no special approving power here: HR's own leave goes through the same queue as anyone else's, and
      HR cannot decide anyone else's leave either. Nobody approves their own leave. See can_decide().
"""
from datetime import date, datetime, timedelta, timezone

from app.hr.leave import store as leave_store
from app.identity.auth import parse_timestamp

BACKDATE_LIMIT_DAYS = 7
LEAVE_YEAR_START_MONTH = 1      # the leave year starts on 1 January (keep equal to leave_year_start_month in the get_leave_balances SQL function)
COMPANY_TIME_ZONE = timezone(timedelta(hours=5, minutes=30))   # India time

PM_DEPARTMENT = "PM"            # the Project Manager approves leave when an employee has no manager_id

APPLY_LEAVE_ACTION = "apply_leave"
CANCEL_LEAVE_ACTION = "cancel_leave"


class LeaveError(Exception):
    """A problem to show to the employee, with the HTTP status code to use on the website API."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_today():
    """Today's date in India. A separate function so tests can control the clock."""
    return datetime.now(COMPANY_TIME_ZONE).date()


def leave_year_label(any_date):
    """The name of the leave year a date belongs to, for example 2026-11-09 gives '2026'."""
    first_year = any_date.year if any_date.month >= LEAVE_YEAR_START_MONTH else any_date.year - 1
    return str(first_year)


def get_now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ small helpers
def parse_date(date_text, field_label):
    try:
        return date.fromisoformat(str(date_text))
    except ValueError:
        raise LeaveError(f"I could not read the {field_label}. Please give it like 2026-11-10.")


def format_date(value):
    return f"{value.strftime('%a')}, {value.day} {value.strftime('%b %Y')}"      # Tue, 10 Nov 2026


def format_days(days):
    return f"{days:g} day" + ("" if days == 1 else "s")


def describe_leave(leave_type_name, start_date, end_date, day_type, days):
    """One readable sentence for a leave, used in confirmation messages and lists."""
    if day_type == "half":
        return f"{leave_type_name} on {format_date(start_date)} (half day)"
    if start_date == end_date:
        return f"{leave_type_name} on {format_date(start_date)} (full day)"
    return f"{leave_type_name} from {format_date(start_date)} to {format_date(end_date)} ({format_days(days)})"


def describe_leave_row(leave_row, leave_types):
    leave_type_name = leave_types.get(leave_row["leave_type"], {}).get("name", leave_row["leave_type"])
    return describe_leave(leave_type_name, date.fromisoformat(leave_row["start_date"]), date.fromisoformat(leave_row["end_date"]),
                          leave_row["day_type"], float(leave_row["days"]))


# ------------------------------------------------------------------ checking a new leave
def check_new_leave(employee_row, leave_type_code, start_date, end_date, day_type, applied_by_chat, document_path=None,
                    skip_backdate_limit=False, require_document=True, person_label=None):
    """
    Check every rule and return the details to save, or raise LeaveError with a message for the employee.
    Nothing is saved here. person_label is the employee's name when someone else (the super admin) applies for them; the messages
    then talk about that person ("Ann has only ...") instead of "you".
    """
    who = person_label or "You"
    has = "has" if person_label else "have"
    if not employee_row or not employee_row["is_active"]:
        raise LeaveError(f"{person_label}'s employee record is not active." if person_label else "Your employee record is not active.", 403)

    leave_types = leave_store.get_leave_types()
    leave_type = leave_types.get(str(leave_type_code or "").strip().lower())
    if leave_type is None:
        available_names = [t["code"] for t in leave_types.values()
                           if t["applies_to_gender"] in (None, employee_row.get("gender")) and t["can_apply_by_chat"]]
        raise LeaveError(f"I do not know that leave type. You can choose: {', '.join(available_names)}.")
    if leave_type["applies_to_gender"] and leave_type["applies_to_gender"] != employee_row.get("gender"):
        raise LeaveError(f"{leave_type['name']} is not available for {person_label or 'you'}.")
    if applied_by_chat and not leave_type["can_apply_by_chat"]:
        raise LeaveError(f"{leave_type['name']} cannot be applied in the chat. Please apply on the website, with the supporting document.")
    if require_document and not leave_type["can_apply_by_chat"] and not document_path:
        raise LeaveError(f"{leave_type['name']} needs a supporting document. Please upload one.")

    if day_type not in ("full", "half"):
        raise LeaveError("The day type must be full or half.")
    if end_date < start_date:
        raise LeaveError("The end date is before the start date.")
    if day_type == "half" and start_date != end_date:
        raise LeaveError("A half day leave can only be for a single date.")

    today = get_today()
    earliest_allowed_date = today - timedelta(days=BACKDATE_LIMIT_DAYS)
    if not skip_backdate_limit and start_date < earliest_allowed_date:
        raise LeaveError(f"You can apply for past dates only up to {BACKDATE_LIMIT_DAYS} days back. "
                         f"The earliest date allowed today is {format_date(earliest_allowed_date)}.")

    maximum_days = leave_type["max_days_per_request"]
    number_of_dates = (end_date - start_date).days + 1
    if maximum_days is not None and number_of_dates > float(maximum_days):
        raise LeaveError(f"One request can cover at most {float(maximum_days):g} consecutive days. "
                         "Please split it into two requests.")

    days = leave_store.count_leave_days(start_date, end_date, day_type, leave_type["count_mode"])
    if days <= 0:
        raise LeaveError("Those dates fall on a weekend or a company holiday, so no leave is needed.")

    document_threshold = leave_type.get("require_document_above_days")
    if require_document and document_threshold is not None and days > float(document_threshold) and not document_path:
        raise LeaveError(f"{leave_type['name']} needs a supporting document when it is for more than "
                         f"{format_days(float(document_threshold))}. Please upload one.")

    overlapping = leave_store.find_overlapping_requests(employee_row["id"], start_date, end_date)
    if overlapping:
        first = overlapping[0]
        raise LeaveError(f"{who} already {has} a {first['status']} {first['leave_type']} leave from "
                         f"{first['start_date']} to {first['end_date']} that overlaps these dates.")

    if leave_type["limit_type"] == "per_year":
        balance = next((row for row in leave_store.get_leave_balances(employee_row["id"], start_date)
                        if row["type_code"] == leave_type["code"]), None)
        remaining_days = float(balance["remaining_days"]) if balance else 0.0
        if days > remaining_days:
            raise LeaveError(f"{who} {has} only {format_days(remaining_days)} of {leave_type['name']} left for {leave_year_label(start_date)}, "
                             f"and this request needs {format_days(days)}.")
    elif leave_type["limit_type"] == "per_request":
        if days > float(leave_type["days_allowed"]):
            raise LeaveError(f"{leave_type['name']} allows at most {float(leave_type['days_allowed']):g} days per request.")
    # "unlimited" (Work From Home): no yearly quota and no per-request day cap here - only
    # max_days_per_request (checked earlier) limits a single request's length.

    return {
        "leave_type": leave_type["code"],
        "leave_type_name": leave_type["name"],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "day_type": day_type,
        "days": days,
        "document_path": document_path,
    }


def refuse_superadmin(person):
    """The super admin is an admin account only: it has no leave of its own (it can apply leave for others)."""
    if person.get("role") == "superadmin":
        raise LeaveError("The super admin has no leave of its own.", 403)


def is_pm(person):
    """
    True for someone whose department is PM (any capital letters), or whose login role is pm.
    An employee row has only "dept"; the logged-in dictionary has "dept" and "role".
    """
    return str(person.get("dept") or "").strip().lower() == PM_DEPARTMENT.lower() or person.get("role") == "pm"


def find_approver_name(employee_row, leave_type_code):
    """Who will see this request: the super admin, always - every leave type is decided only by them."""
    return "the super admin"


def save_leave_request(employee_row, details, reason=None):
    """Save an already-checked leave as a pending request."""
    try:
        saved_row = leave_store.insert_leave_request({
            "employee_id": employee_row["id"],
            "leave_type": details["leave_type"],
            "start_date": details["start_date"],
            "end_date": details["end_date"],
            "day_type": details["day_type"],
            "days": details["days"],
            "reason": reason,
            "document_path": details.get("document_path"),
        })
    except leave_store.LeaveOverlapError:
        raise LeaveError("You already have another leave on these dates.", 409)
    return {"leave": saved_row, "approver_name": find_approver_name(employee_row, details["leave_type"])}


# ------------------------------------------------------------------ cancelling
def check_cancellable(employee_row, leave_id):
    """Return the leave row if this employee may cancel it. Only a still-pending leave qualifies - once
    the super admin has approved (or rejected) it, the employee can no longer undo it themselves, even
    if it has not started yet. Only the super admin can change it from there."""
    leave_row = leave_store.get_leave_request(leave_id)
    if leave_row is None or leave_row["employee_id"] != employee_row["id"]:
        raise LeaveError("I could not find that leave request in your leaves.", 404)
    if leave_row["status"] == "pending":
        return leave_row
    raise LeaveError(f"A {leave_row['status']} leave cannot be cancelled. Once the super admin has decided it, only the super admin can change it.")


def cancel_leave(employee_row, leave_id):
    leave_row = check_cancellable(employee_row, leave_id)
    updated_row = leave_store.update_leave_request(
        leave_id, {"status": "cancelled", "decided_at": get_now().isoformat()}, expected_status=leave_row["status"])
    if updated_row is None:
        raise LeaveError("That leave was just changed by someone else. Please check your leaves again.", 409)
    return updated_row


# ------------------------------------------------------------------ approving (manager or HR)
def can_decide(approver, leave_row, requester_row=None, leave_type=None):
    """
    approver is the logged-in employee dictionary (it has "role" and "employee_id"). Every leave, of
    every type, is decided only by the super admin now - not HR, not the reporting manager, not the
    PM. HR is treated like any other employee here, including for its own leave. Nobody decides their
    own leave (moot for the super admin, which has none of its own - see refuse_superadmin).
    """
    if leave_row["employee_id"] == approver["employee_id"]:
        return False
    return approver["role"] == "superadmin"


def list_approvals(approver):
    """Pending leaves this person may decide, with the employee's name. Only the super admin decides any leave."""
    if approver["role"] != "superadmin":
        return []
    pending_rows = leave_store.list_pending_requests()
    employees = leave_store.get_employees_by_ids([row["employee_id"] for row in pending_rows])
    leave_types = leave_store.get_leave_types()
    approvals = []
    for leave_row in pending_rows:
        requester = employees.get(leave_row["employee_id"], {})
        if not can_decide(approver, leave_row, requester, leave_types.get(leave_row["leave_type"], {})):
            continue
        approvals.append({
            "id": leave_row["id"],
            "emp_id": requester.get("emp_id"),
            "full_name": requester.get("full_name"),
            "leave_type": leave_row["leave_type"],
            "description": describe_leave_row(leave_row, leave_types),
            "reason": leave_row["reason"],
            "has_document": bool(leave_row["document_path"]),
            "requested_at": leave_row["created_at"],
        })
    return approvals


def decide_leave(approver, leave_id, decision, note=None):
    """decision is "approved" or "rejected". Returns the updated leave row."""
    leave_row = leave_store.get_leave_request(leave_id)
    if leave_row is None:
        raise LeaveError("Leave request not found.", 404)
    if leave_row["status"] != "pending":
        raise LeaveError(f"This leave is already {leave_row['status']}.", 409)

    requester = leave_store.get_employee_row(leave_row["employee_id"]) or {}
    leave_type = leave_store.get_leave_types().get(leave_row["leave_type"], {})
    if leave_row["employee_id"] == approver["employee_id"]:
        raise LeaveError("You cannot approve or reject your own leave.", 403)
    if not can_decide(approver, leave_row, requester, leave_type):
        raise LeaveError("Only the super admin can decide this leave.", 403)
    if decision == "rejected" and not (note or "").strip():
        raise LeaveError("Please write the reason for rejecting this leave.", 400)

    updated_row = leave_store.update_leave_request(
        leave_id,
        {"status": decision, "approver_id": approver["employee_id"], "decided_at": get_now().isoformat(), "decision_note": note},
        expected_status="pending",
    )
    if updated_row is None:
        raise LeaveError("This leave was just decided by someone else.", 409)
    return updated_row


def check_can_read_document(reader, leave_id):
    """Return the document path if this person may open it: the owner, or anyone who may decide the leave."""
    leave_row = leave_store.get_leave_request(leave_id)
    if leave_row is None or not leave_row["document_path"]:
        raise LeaveError("No document found for this leave.", 404)
    if leave_row["employee_id"] == reader["employee_id"]:
        return leave_row["document_path"]
    requester = leave_store.get_employee_row(leave_row["employee_id"]) or {}
    leave_type = leave_store.get_leave_types().get(leave_row["leave_type"], {})
    if can_decide(reader, leave_row, requester, leave_type):
        return leave_row["document_path"]
    raise LeaveError("You are not allowed to open this document.", 403)


# ------------------------------------------------------------------ pending actions
def is_expired(action):
    expires_at = parse_timestamp(action["expires_at"])
    return expires_at is not None and expires_at < get_now()


def check_document_belongs(employee_row, document_path):
    """Uploaded documents are stored under the uploader's own id, so nobody can attach someone else's file."""
    if not document_path.startswith(f"{employee_row['id']}/"):
        raise LeaveError("That document does not belong to you.", 403)


def apply_on_website(employee_row, leave_type_code, start_date, end_date, day_type, reason, document_path):
    """The website form: the same rules as the chat, but pressing Submit is the confirmation. Maternity needs a document."""
    if document_path:
        check_document_belongs(employee_row, document_path)
    details = check_new_leave(employee_row, leave_type_code, start_date, end_date or start_date, day_type,
                              applied_by_chat=False, document_path=document_path)
    return save_leave_request(employee_row, details, reason)


def attach_document_to_action(employee_row, action_id, document_path):
    """The employee uploaded a document while a leave is waiting for 'yes'."""
    check_document_belongs(employee_row, document_path)
    action = leave_store.get_action_by_id(action_id)
    if action is None or action["employee_id"] != employee_row["id"] or action["status"] != "waiting":
        raise LeaveError("There is no leave waiting for confirmation.", 404)
    if action["action_type"] != APPLY_LEAVE_ACTION:
        raise LeaveError("A document can only be added to a leave request.")
    payload = dict(action["payload"], document_path=document_path)
    if leave_store.update_action(action_id, {"payload": payload}) is None:
        raise LeaveError("That request is no longer waiting.", 409)


def run_confirmed_action(employee_row, action):
    """Do what the employee confirmed with 'yes'. The rules are checked again, because time has passed."""
    payload = action["payload"]
    if action["action_type"] == APPLY_LEAVE_ACTION:
        details = check_new_leave(
            employee_row, payload["leave_type"], date.fromisoformat(payload["start_date"]), date.fromisoformat(payload["end_date"]),
            payload["day_type"], applied_by_chat=True, document_path=payload.get("document_path"))
        result = save_leave_request(employee_row, details, reason=payload.get("reason"))
        description = describe_leave(details["leave_type_name"], date.fromisoformat(details["start_date"]),
                                     date.fromisoformat(details["end_date"]), details["day_type"], details["days"])
        return (f"Done. Your request #{result['leave']['id']} for {description} was sent to "
                f"{result['approver_name']} for approval.")
    if action["action_type"] == CANCEL_LEAVE_ACTION:
        cancelled_row = cancel_leave(employee_row, payload["leave_id"])
        return f"Done. Your leave request #{cancelled_row['id']} was cancelled."
    raise LeaveError("I do not know how to do that action.")
