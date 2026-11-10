"""
payroll_service.py - who may see which salary, and how it is shown.

Rules:
    - Everyone can see their OWN salary breakdown and payslips.
    - Only HR can ask about somebody else, except the CEO and the CTO (confidential, see people_service.py).
    - Anyone else asking about another person gets the same short reply as for an unknown person,
      so the reply never confirms that the person exists.
    The AI only picks the tool. This code decides what is shown, and nothing here is sent to the AI.
"""
from datetime import date

import payroll_store
import storage_store

NO_ANSWER = "I don't have an answer for this question."
PAYSLIPS_BUCKET = "payslips"


class PayrollError(Exception):
    """A message for the employee (for example: no payslip for that month)."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def format_money(amount):
    amount = float(amount)
    return f"Rs.{int(amount)}" if amount == int(amount) else f"Rs.{amount:.2f}"


def format_month(pay_month):
    return pay_month.strftime("%B %Y")


def parse_month(month_text):
    """'2026-10' -> date(2026, 10, 1). Empty -> None (meaning: the newest payslip)."""
    month_text = (month_text or "").strip()
    if not month_text:
        return None
    try:
        year_text, month_number_text = month_text.split("-")[:2]
        return date(int(year_text), int(month_number_text), 1)
    except (ValueError, TypeError):
        raise PayrollError("I could not read the month. Please say it like: payslip for October 2026.")


# ------------------------------------------------------------------ salary
def build_own_salary_text(details, components):
    if details["salary"] is None:
        return "Your salary is not recorded yet. Please contact HR."
    lines = [f"Your monthly salary is {format_money(details['salary'])}."]
    earnings = [c for c in components if c["component_type"] == "earning"]
    deductions = [c for c in components if c["component_type"] == "deduction"]
    if earnings:
        lines.append("Earnings: " + ", ".join(f"{c['component_name']} {format_money(c['monthly_amount'])}" for c in earnings) + ".")
    if deductions:
        lines.append("Deductions: " + ", ".join(f"{c['component_name']} {format_money(c['monthly_amount'])}" for c in deductions) + ".")
    return "\n".join(lines)


def answer_salary_question(requester, person_text):
    """
    requester is the logged-in employee dictionary. person_text is who the question is about:
    empty or "me" means the requester. Returns the sentence to show.
    """
    import people_service                                   # imported here because people_service also imports this file

    target = people_service.resolve_target(requester, person_text)       # raises PeopleError when not allowed
    if target is None:
        details = payroll_store.get_salary_details(requester["employee_id"])
        return build_own_salary_text(details, payroll_store.list_salary_components(requester["employee_id"]))

    details = payroll_store.get_salary_details(target["id"])
    if details["salary"] is None:
        return f"The salary of {details['full_name']} ({details['emp_id']}) is not recorded yet."
    return f"{format_money(details['salary'])}/Month is the salary of {details['full_name']} ({details['emp_id']})."


# ------------------------------------------------------------------ payslips
def answer_payslip_question(requester, month_text):
    """A temporary link to the employee's OWN payslip (the newest one when no month is given)."""
    pay_month = parse_month(month_text)
    payslip = payroll_store.get_payslip(requester["employee_id"], pay_month)
    if payslip is None:
        if pay_month:
            return f"I could not find your payslip for {format_month(pay_month)}. Please contact HR."
        return "I could not find any payslip for you yet. Please contact HR."
    link = storage_store.create_signed_link(PAYSLIPS_BUCKET, payslip["file_path"])
    month_label = format_month(date.fromisoformat(payslip["pay_month"]))
    return (f"Here is your payslip for {month_label}: {link}\n"
            f"The link works for {storage_store.SIGNED_LINK_SECONDS // 60} minutes. Ask again for a new one.")


def check_payslip_upload(emp_id, month_text):
    """HR upload: returns (employee details, first day of the month) or raises PayrollError."""
    pay_month = parse_month(month_text)
    if pay_month is None:
        raise PayrollError("The month is required, like 2026-10.")
    details = payroll_store.find_employee_by_text(emp_id)
    if details is None:
        raise PayrollError("No employee found with this ID.", 404)
    return details, pay_month
