"""
requests.py - the shape of what a client SENDS.

One model per endpoint that takes a body. Each field carries its own size and
type limits, so FastAPI rejects a malformed request with a 422 before any
handler, service or database call runs.

Nothing here contains a rule: a model says "a password is 1 to 200 characters",
never "a password must have a digit". That belongs in identity/auth.py.
"""
import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.api.limits import MAXIMUM_PASSWORD_INPUT_LENGTH, MAXIMUM_QUESTION_LENGTH


# ------------------------------------------------------------------ logins
class LoginRequest(BaseModel):
    emp_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)


class RegisterRequest(BaseModel):
    emp_id: str = Field(min_length=1, max_length=50)
    role: Literal["employee", "hr", "ceo", "pm"] = "employee"


class ResetPasswordRequest(BaseModel):
    emp_id: str = Field(min_length=1, max_length=50)


# ------------------------------------------------------------------ chat
class ChatRequest(BaseModel):
    """What the client sends to /chat."""

    question: str = Field(min_length=1, max_length=MAXIMUM_QUESTION_LENGTH)


# ------------------------------------------------------------------ attendance
class AttendanceCorrectionRequest(BaseModel):
    """A missed check-in or check-out. The times are 24 hour India time, for example 10:05 or 19:10."""

    work_date: datetime.date
    check_in_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    check_out_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    reason: str = Field(min_length=3, max_length=300)


class SuperadminLeaveRequest(BaseModel):
    """Leave applied by the super admin for someone. It is approved at once."""

    emp_id: str = Field(min_length=1, max_length=50)
    leave_type: str = Field(min_length=1, max_length=30)
    start_date: datetime.date
    end_date: datetime.date | None = None
    day_type: Literal["full", "half"] = "full"
    reason: str | None = Field(default=None, max_length=500)


class SuperadminAttendanceRequest(BaseModel):
    """The times (24 hour India time) the super admin sets for one person on one day."""

    emp_id: str = Field(min_length=1, max_length=50)
    work_date: datetime.date
    check_in_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    check_out_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class CorrectionDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=300)


# ------------------------------------------------------------------ leave
class LeaveApplyRequest(BaseModel):
    leave_type: str = Field(min_length=1, max_length=30)
    start_date: datetime.date
    end_date: datetime.date | None = None
    day_type: Literal["full", "half"] = "full"
    reason: str | None = Field(default=None, max_length=500)
    document_path: str | None = Field(default=None, max_length=300)   # from POST /leave/documents


class LeaveDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=500)


class AttachDocumentRequest(BaseModel):
    document_path: str = Field(min_length=1, max_length=300)
