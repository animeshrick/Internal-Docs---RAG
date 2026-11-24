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

from app.api.limits import (
    MAXIMUM_GOVERNMENT_ID_NUMBER_LENGTH,
    MAXIMUM_MESSAGE_BODY_LENGTH,
    MAXIMUM_PASSWORD_INPUT_LENGTH,
    MAXIMUM_QUESTION_LENGTH,
)


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


class WhatsAppLookupRequest(BaseModel):
    """Turn 1 of the WhatsApp reset: is this number one of ours? Sent by the n8n workflow."""

    phone_number: str = Field(min_length=1, max_length=30)


class WhatsAppVerifyRequest(BaseModel):
    """Turn 2: the current password the employee typed into WhatsApp, with the number it came from."""

    phone_number: str = Field(min_length=1, max_length=30)
    current_password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)


class WhatsAppSetPasswordRequest(BaseModel):
    """Turn 3: the password the employee chose, plus the ticket turn 2 handed back."""

    phone_number: str = Field(min_length=1, max_length=30)
    reset_ticket: str = Field(min_length=1, max_length=100)
    new_password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)


class CreateEmployeeRequest(BaseModel):
    """A brand-new employee row. HR fills this in once, then calls /register to create a login for them."""

    emp_id: str = Field(min_length=1, max_length=50)
    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=200)
    dept: str | None = Field(default=None, max_length=50)
    phone: str | None = Field(default=None, max_length=20)
    gender: str | None = Field(default=None, max_length=20)
    date_of_birth: datetime.date | None = None
    date_of_joining: datetime.date | None = None
    salary: float | None = Field(default=None, ge=0)
    is_confidential: bool = False


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


# ------------------------------------------------------------------ attendance admin (office network)
class NetworkCheckRequest(BaseModel):
    """Turn the office-network restriction on check-in and check-out on or off."""

    enabled: bool


class OfficeNetworkRequest(BaseModel):
    """One office network to check in and out from, for example 203.0.113.0/24."""

    label: str = Field(min_length=1, max_length=100)
    cidr: str = Field(min_length=1, max_length=50)


class WhatsAppNumberRequest(BaseModel):
    """One number to receive the twice-daily WhatsApp HR report, for example +919876543210."""

    label: str = Field(min_length=1, max_length=100)
    phone_number: str = Field(min_length=1, max_length=20)


class OfficeNetworkActiveRequest(BaseModel):
    is_active: bool


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


# ------------------------------------------------------------------ shared documents
class DocumentAccessDecisionRequest(BaseModel):
    decision: Literal["approved", "denied"]


# ------------------------------------------------------------------ 1-to-1 messages
class SendMessageRequest(BaseModel):
    """A message to one other person. "attachment_path" (if any) comes from POST /messages/attachments."""

    to_emp_id: str = Field(min_length=1, max_length=50)
    body: str | None = Field(default=None, max_length=MAXIMUM_MESSAGE_BODY_LENGTH)
    attachment_path: str | None = Field(default=None, max_length=300)
    attachment_filename: str | None = Field(default=None, max_length=120)
    attachment_content_type: str | None = Field(default=None, max_length=100)


# ------------------------------------------------------------------ government ID (Aadhaar / PAN)
class GovernmentIdRequest(BaseModel):
    """
    The one-time government ID an employee submits. The images are uploaded first
    (POST /government-id/images) and their paths are sent here.

    Aadhaar is a two-sided card, so it needs both image paths; PAN is front-only, so back_image_path
    stays empty. Whether the number itself is a real Aadhaar / PAN number is checked in
    app/hr/government_id/service.py, not here.
    """

    id_type: Literal["aadhaar", "pan"]
    id_number: str = Field(min_length=1, max_length=MAXIMUM_GOVERNMENT_ID_NUMBER_LENGTH)
    front_image_path: str = Field(min_length=1, max_length=300)
    back_image_path: str | None = Field(default=None, max_length=300)
