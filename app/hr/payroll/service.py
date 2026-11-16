"""
hr/payroll/service.py - who may see which salary, and how it is shown.

Rules:
    - Everyone can see their OWN salary breakdown and payslips.
    - Only HR can ask about somebody else, except the CEO and the CTO (confidential, see hr/people/service.py).
    - Anyone else asking about another person gets the same short reply as for an unknown person,
      so the reply never confirms that the person exists.
    The AI only picks the tool. This code decides what is shown, and nothing here is sent to the AI.
"""
import calendar
import uuid
from datetime import date

from app.hr.payroll import pdf as payroll_pdf
from app.hr.payroll import store as payroll_store
from app.files import storage as storage_store

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


def get_today():
    """A thin wrapper so tests can fix "today" without touching the clock."""
    return date.today()


def add_month(month):
    """The first day of the month after this one. month is always the 1st already."""
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def _month_in_employment(details, pay_month):
    """True when pay_month falls between this employee's joining month and the current month, inclusive."""
    if not details or not details.get("doj"):
        return False
    doj = date.fromisoformat(details["doj"])
    today = get_today()
    return date(doj.year, doj.month, 1) <= pay_month <= date(today.year, today.month, 1)


def _compute_net_pay(salary, components):
    """Earnings minus deductions when a breakdown is configured, otherwise the flat monthly salary."""
    earnings = sum(float(c["monthly_amount"]) for c in components if c["component_type"] == "earning")
    deductions = sum(float(c["monthly_amount"]) for c in components if c["component_type"] == "deduction")
    if earnings or deductions:
        return earnings - deductions
    return float(salary) if salary is not None else 0.0


def payable_days_in_month(pay_month):
    """The number of calendar days in pay_month (the slip's "Payable Days" - leave/LOP is not
    subtracted from it, at least for now)."""
    return calendar.monthrange(pay_month.year, pay_month.month)[1]


def _generate_payslip_file(employee_id, details, pay_month):
    """Builds the PDF, uploads it, saves the payslips row, and returns the stored file path."""
    components = payroll_store.list_salary_components(employee_id)
    net_pay = _compute_net_pay(details["salary"], components)
    pdf_bytes = payroll_pdf.build_payslip_pdf(details, components, format_month(pay_month), net_pay, payable_days_in_month(pay_month))
    file_path = f"{employee_id}/{pay_month.isoformat()}-{uuid.uuid4().hex}.pdf"
    storage_store.upload_file(PAYSLIPS_BUCKET, file_path, pdf_bytes, "application/pdf")
    payroll_store.save_payslip(employee_id, pay_month, net_pay, file_path)
    return file_path


def list_my_payslip_years(employee):
    """Every calendar year from this employee's joining year to the current year, newest first."""
    details = payroll_store.get_salary_details(employee["employee_id"])
    if not details or not details.get("doj"):
        return []
    doj_year = date.fromisoformat(details["doj"]).year
    current_year = get_today().year
    return list(range(current_year, doj_year - 1, -1))


def list_my_payslip_months(employee, year=None):
    """January through December of one year (the current year when none is given), each marked with
    whether a payslip already exists and, if the month falls outside employment, why it's disabled:
    "future" for a month after the current one, "before_joining" for a month before this employee
    joined. A disabled month is still shown (so the year reads as a full calendar), just with no
    download/generate action available."""
    details = payroll_store.get_salary_details(employee["employee_id"])
    if not details or not details.get("doj"):
        return []
    doj_month = date.fromisoformat(details["doj"]).year, date.fromisoformat(details["doj"]).month
    doj_month = date(*doj_month, 1)
    today = get_today()
    current_month = date(today.year, today.month, 1)
    target_year = year if year is not None else today.year
    existing_months = {row["pay_month"] for row in payroll_store.list_payslips(employee["employee_id"])}

    result = []
    for month_number in range(1, 13):
        month = date(target_year, month_number, 1)
        if month > current_month:
            disabled_reason = "future"
        elif month < doj_month:
            disabled_reason = "before_joining"
        else:
            disabled_reason = None
        result.append({
            "month": month.strftime("%Y-%m"),
            "label": format_month(month),
            "available": month.isoformat() in existing_months,
            "disabled_reason": disabled_reason,
        })
    return result


def get_or_build_payslip_link(employee, month_text):
    """The employee's own payslip for one month: generated on first request, reused after that."""
    pay_month = parse_month(month_text)
    if pay_month is None:
        raise PayrollError("The month is required, like 2026-10.")
    details = payroll_store.get_salary_details(employee["employee_id"])
    if not _month_in_employment(details, pay_month):
        raise PayrollError("That month is outside your employment period.", 400)

    existing = payroll_store.get_payslip(employee["employee_id"], pay_month)
    if existing is not None:
        file_path = existing["file_path"]
    else:
        if details["salary"] is None:
            raise PayrollError("Your salary is not recorded yet. Please contact HR.", 404)
        file_path = _generate_payslip_file(employee["employee_id"], details, pay_month)

    return {"url": storage_store.create_signed_link(PAYSLIPS_BUCKET, file_path), "valid_for_seconds": storage_store.SIGNED_LINK_SECONDS}


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
    from app.hr.people import service as people_service                                   # imported here because people_service also imports this file

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
