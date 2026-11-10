"""
FastAPI app for the HR policy chatbot.

Run from the project root:
    uvicorn main:app --app-dir app --reload

Then open http://127.0.0.1:8000/docs to try it in the browser.

Endpoints:
    GET  /health                          open to everyone
    POST /login                           emp_id + password -> token
    POST /change-password                 any logged-in employee (also the first step with a temporary password)
    GET  /me                              who am I
    POST /chat                            policy questions + my leave (apply / cancel with "reply yes to confirm")
    GET  /leave/balances                  my leave numbers
    GET  /leave/mine                      my leave requests
    POST /leave/documents                 upload a PDF / PNG / JPG for a leave (raw file in the body)
    POST /leave/apply                     website form (maternity is only possible here)
    POST /leave/{id}/cancel               cancel my leave
    GET  /leave/approvals                 leaves waiting for me (my team, or everyone for HR)
    POST /leave/{id}/decision             approve or reject (reporting manager or HR)
    GET  /leave/{id}/document-url         temporary link to a leave document
    POST /pending-actions/{id}/document   attach an uploaded document to the leave waiting for "yes"
    POST /payroll/payslips                HR only: upload a payslip PDF for an employee and month
    POST /register                        HR only: create a login for an existing employee
    POST /reset-password                  HR only: give an employee a new temporary password

There is no public sign-up. HR creates every login.
"""

import datetime
import logging
import os
import re
import uuid
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import auth
import leave_service
import leave_store
import payroll_service
import payroll_store
import storage_store
from assistant import answer_chat_message
import chat_store
import people_service
from auth import AuthError
from leave_service import LeaveError
from payroll_service import PayrollError

logger = logging.getLogger("hr_chatbot")

MAXIMUM_QUESTION_LENGTH = 1000
MAXIMUM_PASSWORD_INPUT_LENGTH = 200   # only stops huge inputs; the real rules are in auth.py
MAXIMUM_DOCUMENT_BYTES = 5 * 1024 * 1024
ALLOWED_DOCUMENT_TYPES = ("application/pdf", "image/png", "image/jpeg")

app = FastAPI(title="HR Policy Chatbot")

# The website is normally served from the same address (through a proxy), so CORS stays off by default.
# If the website runs on its own address, list it in .env, for example:
#   CORS_ALLOWED_ORIGINS=https://hr.example.com,http://localhost:5173
allowed_origins = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
if allowed_origins:
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"], expose_headers=["Retry-After"])

# auto_error=False so that a missing token gives our own 401 message (the default would be 403).
bearer_scheme = HTTPBearer(auto_error=False)


@app.exception_handler(AuthError)
def handle_auth_error(request, error):
    """Turn an AuthError raised anywhere into a normal JSON error response."""
    headers = {}
    if error.status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if error.status_code == 429:
        headers["Retry-After"] = str(auth.LOCK_MINUTES * 60)      # seconds; the lock lasts this long
    return JSONResponse(status_code=error.status_code, content={"detail": error.message, "code": error.code},
                        headers=headers or None)


@app.exception_handler(PayrollError)
def handle_payroll_error(request, error):
    return JSONResponse(status_code=error.status_code, content={"detail": error.message, "code": "payroll"})


@app.exception_handler(LeaveError)
def handle_leave_error(request, error):
    """A broken leave rule (for example "only 3 days left") becomes a normal JSON error."""
    return JSONResponse(status_code=error.status_code, content={"detail": error.message, "code": "leave_rule"})


# ------------------------------------------------------------------ who is calling?
def get_current_employee(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """Any valid login token. Used by /change-password."""
    if credentials is None:
        raise AuthError(401, "Please log in first.")
    return auth.load_employee_from_token(credentials.credentials)


def get_ready_employee(employee: dict = Depends(get_current_employee)):
    """A valid token AND the employee has already replaced the temporary password."""
    if employee["must_change_password"]:
        raise AuthError(403, "Please change your temporary password first (POST /change-password).", "must_change_password")
    return employee


def require_hr(employee: dict = Depends(get_ready_employee)):
    """Only HR may continue."""
    if employee["role"] != "hr":
        raise AuthError(403, "Only HR can do this.")
    return employee


# ------------------------------------------------------------------ request and response shapes
class LoginRequest(BaseModel):
    emp_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    must_change_password: bool
    role: str
    full_name: str
    emp_id: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_INPUT_LENGTH)


class RegisterRequest(BaseModel):
    emp_id: str = Field(min_length=1, max_length=50)
    role: Literal["employee", "hr", "ceo", "pm"] = "employee"


class ResetPasswordRequest(BaseModel):
    emp_id: str = Field(min_length=1, max_length=50)


class CredentialsResponse(BaseModel):
    """The temporary password is shown only in this response. HR passes it to the employee."""

    emp_id: str
    full_name: str
    role: str
    temporary_password: str


MESSAGES_SHOWN_TO_THE_AI = 6      # the AI reads the last 3 questions and 3 answers


class ChatRequest(BaseModel):
    """What the client sends to /chat."""

    question: str = Field(min_length=1, max_length=MAXIMUM_QUESTION_LENGTH)


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


# ------------------------------------------------------------------ endpoints
@app.get("/health")
def health_check():
    """Tells a load balancer or a person that the server is running."""
    return {"status": "ok"}


@app.post("/login", response_model=LoginResponse)
def login(request: LoginRequest):
    """Employee id + password in, token out."""
    return auth.authenticate(request.emp_id, request.password)


@app.post("/change-password")
def change_password(request: ChangePasswordRequest, employee: dict = Depends(get_current_employee)):
    """Set your own password. Afterwards every old token stops working, so log in again."""
    auth.change_password(employee["employee_id"], request.current_password, request.new_password)
    return {"message": "Password changed. Please log in again with your new password."}


@app.post("/register", response_model=CredentialsResponse)
def register(request: RegisterRequest, hr_employee: dict = Depends(require_hr)):
    """HR only. Creates the login for an employee that already exists in the employee table."""
    result = auth.create_credentials(request.emp_id, request.role)
    logger.info("Login created for %s (role %s) by %s", result["emp_id"], result["role"], hr_employee["emp_id"])
    return result


@app.post("/reset-password", response_model=CredentialsResponse)
def reset_password(request: ResetPasswordRequest, hr_employee: dict = Depends(require_hr)):
    """HR only. New temporary password for a forgotten password or a locked account."""
    result = auth.reset_password(request.emp_id)
    logger.info("Password reset for %s by %s", result["emp_id"], hr_employee["emp_id"])
    return result


@app.get("/me")
def read_me(employee: dict = Depends(get_current_employee)):
    """Who is logged in. Works even before the temporary password is changed."""
    return {k: employee[k] for k in ("emp_id", "full_name", "dept", "role", "must_change_password")}


# These are normal "def" endpoints (not "async def") on purpose: they make blocking network calls,
# so FastAPI runs them in a worker thread and other requests are not blocked.
@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, employee: dict = Depends(get_ready_employee)):
    """Answer one message: a policy question, a question about my leave, or a leave action. Needs a login token."""
    logger.info("Chat request from %s", employee["emp_id"])
    asked_at = datetime.datetime.now(datetime.timezone.utc)
    try:
        history = chat_store.list_messages(employee["employee_id"], MESSAGES_SHOWN_TO_THE_AI)
    except Exception:
        logger.exception("Could not read the chat history (the answer continues without it)")
        history = []
    try:
        result = answer_chat_message(employee, request.question, history)
        try:
            chat_store.delete_expired_messages_once_an_hour()
            question_row, answer_row = chat_store.save_exchange(
                employee["employee_id"], request.question.strip(), asked_at, result["answer"], result.get("sources"),
                datetime.datetime.now(datetime.timezone.utc))
            result = dict(result, asked_at=question_row["created_at"], answered_at=answer_row["created_at"])
        except Exception:
            logger.exception("Could not save the chat messages (the answer is still sent)")
        return result
    except ValueError as error:
        # An empty question (for example, only spaces).
        raise HTTPException(status_code=400, detail=str(error))
    except Exception:
        # Log the real error for us, but send the client a safe, generic message.
        logger.exception("Chat request failed")
        raise HTTPException(
            status_code=502,
            detail="The assistant could not answer right now. Please try again in a moment.",
        )


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


@app.get("/me/profile", response_model=ProfileResponse)
def read_my_profile(employee: dict = Depends(get_ready_employee)):
    """My own details. There is no way to ask for anybody else's here."""
    return people_service.read_own_profile(employee)


@app.get("/chat/history", response_model=list[ChatHistoryMessage])
def chat_history(limit: int = Query(50, ge=1, le=100), before_id: int | None = Query(None, ge=1),
                 employee: dict = Depends(get_ready_employee)):
    """My own chat messages of the last 30 days, oldest first. before_id loads the page before that message."""
    return chat_store.list_messages(employee["employee_id"], limit, before_id)


# ------------------------------------------------------------------ leave (website)
def load_employee_row(employee):
    """The full employee row (gender, manager ...) of the logged-in employee."""
    return leave_store.get_employee_row(employee["employee_id"])


@app.get("/leave/balances")
def leave_balances(employee: dict = Depends(get_ready_employee)):
    return leave_store.get_leave_balances(employee["employee_id"], leave_service.get_today())


@app.get("/leave/mine")
def my_leaves(status: Literal["pending", "approved", "rejected", "cancelled"] | None = None,
              employee: dict = Depends(get_ready_employee)):
    leave_types = leave_store.get_leave_types()
    rows = leave_store.list_leave_requests(employee["employee_id"], status)
    return [dict(row, description=leave_service.describe_leave_row(row, leave_types)) for row in rows]


def make_safe_filename(filename):
    """Keep letters, digits, dot, dash and underscore only, so the stored path is always safe."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-80:]


# async because the file arrives in the request body; the blocking upload runs in a worker thread.
@app.post("/leave/documents")
async def upload_leave_document(request: Request, filename: str = Query(min_length=1, max_length=120),
                                employee: dict = Depends(get_ready_employee)):
    """Send the raw file as the body, with Content-Type application/pdf, image/png or image/jpeg."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_DOCUMENT_TYPES:
        raise LeaveError("Only PDF, PNG or JPG files can be uploaded.", 415)
    declared_size = request.headers.get("content-length", "")
    if declared_size.isdigit() and int(declared_size) > MAXIMUM_DOCUMENT_BYTES:
        raise LeaveError("The file is too large. The limit is 5 MB.", 413)

    file_bytes = await request.body()
    if not file_bytes:
        raise LeaveError("The file is empty.", 400)
    if len(file_bytes) > MAXIMUM_DOCUMENT_BYTES:
        raise LeaveError("The file is too large. The limit is 5 MB.", 413)

    document_path = f"{employee['employee_id']}/{uuid.uuid4().hex}-{make_safe_filename(filename)}"
    try:
        await run_in_threadpool(storage_store.upload_file, storage_store.LEAVE_DOCUMENTS_BUCKET, document_path, file_bytes, content_type)
    except Exception:
        logger.exception("Document upload failed")
        raise HTTPException(status_code=502, detail="The document could not be saved right now. Please try again.")
    return {"document_path": document_path}


@app.post("/leave/apply")
def apply_leave(request: LeaveApplyRequest, employee: dict = Depends(get_ready_employee)):
    """Apply on the website. Maternity leave can only be applied here (document required)."""
    result = leave_service.apply_on_website(
        load_employee_row(employee), request.leave_type, request.start_date, request.end_date,
        request.day_type, request.reason, request.document_path)
    return {"leave_id": result["leave"]["id"], "status": "pending", "approver": result["approver_name"],
            "message": f"Your request was sent to {result['approver_name']} for approval."}


@app.post("/leave/{leave_id}/cancel")
def cancel_my_leave(leave_id: int, employee: dict = Depends(get_ready_employee)):
    leave_service.cancel_leave(load_employee_row(employee), leave_id)
    return {"message": "The leave was cancelled."}


@app.get("/leave/approvals")
def leave_approvals(employee: dict = Depends(get_ready_employee)):
    """Leaves waiting for this person: their direct reports (or everyone, for HR)."""
    return leave_service.list_approvals(employee)


@app.post("/leave/{leave_id}/decision")
def decide_leave(leave_id: int, request: LeaveDecisionRequest, employee: dict = Depends(get_ready_employee)):
    updated_row = leave_service.decide_leave(employee, leave_id, request.decision, request.note)
    logger.info("Leave %s %s by %s", leave_id, request.decision, employee["emp_id"])
    return {"leave_id": updated_row["id"], "status": updated_row["status"]}


@app.get("/leave/{leave_id}/document-url")
def leave_document_url(leave_id: int, employee: dict = Depends(get_ready_employee)):
    document_path = leave_service.check_can_read_document(employee, leave_id)
    return {"url": storage_store.create_signed_link(storage_store.LEAVE_DOCUMENTS_BUCKET, document_path),
            "valid_for_seconds": storage_store.SIGNED_LINK_SECONDS}


@app.post("/pending-actions/{action_id}/document")
def attach_document(action_id: int, request: AttachDocumentRequest, employee: dict = Depends(get_ready_employee)):
    """The employee uploaded a document while the chat waits for "yes"."""
    leave_service.attach_document_to_action(load_employee_row(employee), action_id, request.document_path)
    return {"message": "Document added. Reply yes in the chat to confirm your leave."}


# ------------------------------------------------------------------ payslips (HR uploads, employees ask the chat)
@app.post("/payroll/payslips")
async def upload_payslip(request: Request, emp_id: str = Query(min_length=1, max_length=50), month: str = Query(pattern=r"^\d{4}-\d{2}$"),
                         net_pay: float | None = Query(default=None, ge=0), hr_employee: dict = Depends(require_hr)):
    """HR only. Send the PDF as the raw body (Content-Type application/pdf). Employees then get it through the chat."""
    if (request.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/pdf":
        raise PayrollError("Payslips must be PDF files.", 415)
    file_bytes = await request.body()
    if not file_bytes or len(file_bytes) > MAXIMUM_DOCUMENT_BYTES:
        raise PayrollError("The PDF must not be empty and must be at most 5 MB.", 413)

    details, pay_month = payroll_service.check_payslip_upload(emp_id, month)
    file_path = f"{details['id']}/{pay_month.isoformat()}-{uuid.uuid4().hex}.pdf"
    try:
        await run_in_threadpool(storage_store.upload_file, payroll_service.PAYSLIPS_BUCKET, file_path, file_bytes, "application/pdf")
        await run_in_threadpool(payroll_store.save_payslip, details["id"], pay_month, net_pay, file_path)
    except Exception:
        logger.exception("Payslip upload failed")
        raise HTTPException(status_code=502, detail="The payslip could not be saved right now. Please try again.")
    logger.info("Payslip %s for %s uploaded by %s", month, details["emp_id"], hr_employee["emp_id"])
    return {"emp_id": details["emp_id"], "month": month, "message": "Payslip saved."}
