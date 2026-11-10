"""
hr/people/service.py - WHO may see data about WHOM, and the employee profile text.

Rules:
    - Everyone can ask about themselves.
    - Only HR can ask about somebody else, and HR sees everything about that person EXCEPT the CEO and the CTO.
      Their data is confidential: the CEO (role ceo) and anyone with employee.is_confidential = true.
      Asking for "the CEO" or "the CTO" by title is refused without even looking them up.
    - Anyone else asking about another person gets the same short reply as for a protected or unknown person,
      so the reply never confirms that the person exists.
The AI only picks a tool and a name. This code decides, and it runs before any data is read.
"""
import re

from app.hr.leave import service as leave_service
from app.hr.leave import store as leave_store
from app.hr.payroll import service as payroll_service
from app.hr.payroll import store as payroll_store
from app.hr.people import store as people_store

NO_ANSWER = "I don't have an answer for this question."
NOT_FOUND = "I could not find exactly one employee with that name or ID."
SELF_WORDS = ("", "me", "my", "myself", "self", "i", "mine")
PROTECTED_TITLES = re.compile(r"\b(ceo|cto|chief executive|chief technology)\b", re.IGNORECASE)


class PeopleError(Exception):
    """A message to show instead of data."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def simplify(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def is_asking_about_self(requester, person_text):
    text = (person_text or "").strip().lower()
    return text in SELF_WORDS or simplify(text) in (simplify(requester.get("emp_id")), simplify(requester.get("full_name")))


def resolve_target(requester, person_text):
    """
    Returns None when the question is about the requester, or the target employee row.
    Raises PeopleError when the requester may not see that person.
    """
    if is_asking_about_self(requester, person_text):
        return None
    if requester["role"] != "hr":
        raise PeopleError(NO_ANSWER)
    if PROTECTED_TITLES.search(person_text or ""):
        raise PeopleError(NO_ANSWER)

    target = people_store.find_employee(person_text)
    if target is None:
        raise PeopleError(NOT_FOUND)
    if target.get("role") == "ceo" or target.get("is_confidential"):
        raise PeopleError(NO_ANSWER)
    return target


# ------------------------------------------------------------------ my own profile (the /me page)
def read_own_profile(employee):
    """The logged-in employee's own details for the profile page. No salary here (it is in the chat), and nothing about anyone else."""
    row = people_store.get_profile(employee["employee_id"])
    manager = leave_store.get_employee_row(row["manager_id"]) if row.get("manager_id") else None
    return {
        "emp_id": row["emp_id"], "full_name": row["full_name"], "role": employee["role"], "dept": row["dept"],
        "email": row["email"], "phone": row["phone"], "gender": row["gender"], "date_of_joining": row["doj"],
        "date_of_birth": row["dob"], "address": row["full_current_address"], "emergency_contact": row["emergency_ph_no"],
        "manager_name": manager["full_name"] if manager else None, "is_active": row["is_active"],
    }


# ------------------------------------------------------------------ the profile text
def show(value):
    return str(value) if value not in (None, "") else "not recorded"


def build_profile_text(target):
    """Everything HR may know about one employee, as plain text. Built from the database, never by the AI."""
    manager = leave_store.get_employee_row(target["manager_id"]) if target.get("manager_id") else None
    lines = [
        f"{target['full_name']} ({target['emp_id']})",
        f"Department: {show(target['dept'])}. Status: {'active' if target['is_active'] else 'not active'}. "
        f"Reports to: {manager['full_name'] if manager else 'no manager set (the PM approves leave)'}.",
        f"Gender: {show(target['gender'])}. Date of joining: {show(target['doj'])}. Date of birth: {show(target['dob'])}.",
        f"Email: {show(target['email'])}. Phone: {show(target['phone'])}. Emergency contact: {show(target['emergency_ph_no'])}.",
        f"Address: {show(target['full_current_address'])}.",
    ]
    details = payroll_store.get_salary_details(target["id"])
    lines.append(payroll_service.build_own_salary_text(details, payroll_store.list_salary_components(target["id"])).replace("Your", "Their", 1))

    today = leave_service.get_today()
    yearly = [b for b in leave_store.get_leave_balances(target["id"], today) if b["limit_type"] == "per_year"]
    if yearly:
        total_left = sum(float(b["remaining_days"]) for b in yearly)
        parts = ", ".join(f"{b['type_name']} {float(b['remaining_days']):g} left (used {float(b['used_days']):g}, waiting {float(b['pending_days']):g})" for b in yearly)
        lines.append(f"Leave in {leave_service.leave_year_label(today)}: {total_left:g} days left in total. {parts}.")

    leave_types = leave_store.get_leave_types()
    recent = leave_store.list_leave_requests(target["id"], limit=5)
    if recent:
        lines.append("Recent leave requests: " + "; ".join(
            f"#{row['id']} {leave_service.describe_leave_row(row, leave_types)} ({row['status']})" for row in recent) + ".")
    else:
        lines.append("No leave requests yet.")
    return "\n".join(lines)
