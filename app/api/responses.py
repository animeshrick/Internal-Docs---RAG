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
