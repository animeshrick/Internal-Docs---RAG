"""
responses.py - the shape of what the API SENDS BACK.

Each is named in its route's response_model=, which does two jobs: it is the
contract the website can rely on, and it drops any field not listed here, so a
column added to a database table cannot leak out of an endpoint by accident.
That is why the profile and credentials shapes are spelled out field by field.
"""
from typing import Literal

from pydantic import BaseModel


# ------------------------------------------------------------------ logins
class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    must_change_password: bool
    role: str
    full_name: str
    emp_id: str
    # What the website shows: "hr", "pm", "employee", or "none" (the CEO and the CTO have no attendance section).
    attendance_access: str = "none"


class CredentialsResponse(BaseModel):
    """The temporary password is shown only in this response. HR passes it to the employee."""

    emp_id: str
    full_name: str
    role: str
    temporary_password: str


# ------------------------------------------------------------------ chat
class ChatResponse(BaseModel):
    """What /chat sends back. Each source is a dictionary (number, chunk_id, doc_id, title ...)."""

    question: str
    rewritten_question: str
    answer: str
    answerable: bool
    sources: list[dict]
    # Set when the assistant is waiting for "yes": {"id", "action_type", "summary", "allows_document", "expires_at"}
    pending_action: dict | None = None
    # The exact times (UTC) the question and the answer were saved, so the page can show them. None if saving failed.
    asked_at: str | None = None
    answered_at: str | None = None


class ChatHistoryMessage(BaseModel):
    """One saved message (a question or an answer)."""

    id: int
    sender: Literal["user", "assistant"]
    message: str
    sources: list[dict] = []
    created_at: str


# ------------------------------------------------------------------ people
class ProfileResponse(BaseModel):
    """The logged-in employee's own profile. A value is None when it is not recorded."""

    emp_id: str
    full_name: str
    role: str
    dept: str | None = None
    email: str | None = None
    phone: str | None = None
    gender: str | None = None
    date_of_joining: str | None = None
    date_of_birth: str | None = None
    address: str | None = None
    emergency_contact: str | None = None
    manager_name: str | None = None
    is_active: bool


# ------------------------------------------------------------------ attendance
class AttendanceTodayResponse(BaseModel):
    """Today's state for the check-in / check-out buttons. Times are UTC text; the website shows them in India time."""

    work_date: str
    day_note: str | None = None          # why nothing is expected today (weekend, holiday, leave)
    checked_in_at: str | None = None
    checked_out_at: str | None = None
    status: str
    is_late: bool
    is_early_leave: bool
    worked_minutes: int | None = None
    can_check_in: bool
    can_check_out: bool
    office_start: str
    office_end: str
    late_after: str
    network_check_enabled: bool


class AttendanceDay(BaseModel):
    """One day of a calendar. status: present, half_day, absent, incomplete, on_leave, holiday, weekend, checked_in, not_checked_in."""

    date: str
    weekday: str
    status: str
    check_in_at: str | None = None
    check_out_at: str | None = None
    worked_minutes: int | None = None
    is_late: bool = False
    is_early_leave: bool = False
    note: str | None = None


class AttendanceSummary(BaseModel):
    present: int
    half_day: int
    absent: int
    incomplete: int
    on_leave: int
    holiday: int
    weekend: int
    checked_in: int
    not_checked_in: int
    late_days: int
    early_leave_days: int
    worked_minutes: int


class AttendanceMonthResponse(BaseModel):
    emp_id: str
    full_name: str
    month: str
    days: list[AttendanceDay]
    summary: AttendanceSummary


class AttendancePersonDay(AttendanceDay):
    emp_id: str
    full_name: str
    dept: str | None = None


class AttendanceTeamResponse(BaseModel):
    date: str
    people: list[AttendancePersonDay]
    summary: AttendanceSummary


class AttendanceCorrection(BaseModel):
    id: int
    employee_id: int
    emp_id: str | None = None
    full_name: str | None = None
    dept: str | None = None
    work_date: str
    requested_check_in: str | None = None
    requested_check_out: str | None = None
    current_check_in_at: str | None = None
    current_check_out_at: str | None = None
    reason: str
    status: str
    decided_at: str | None = None
    decision_note: str | None = None
    created_at: str
