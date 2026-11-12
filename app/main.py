"""
FastAPI app for the HR policy chatbot.

Run from the project root:
    uvicorn app.main:app --reload

The request and response shapes live in app/api/requests.py and app/api/responses.py;
the input size limits in app/api/limits.py.

Then open http://127.0.0.1:8000/docs to try it in the browser.

Endpoints:
    GET  /health                          open to everyone
    POST /login                           emp_id + password -> token
    POST /change-password                 any logged-in employee (also the first step with a temporary password)
    GET  /me                              who am I
    GET  /me/profile                      my own details
    GET  /attendance/today                my check-in state for today
    POST /attendance/check-in             check in (office network only, when the network check is on)
    POST /attendance/check-out            check out
    GET  /attendance/mine                 my month (?month=2026-11)
    GET  /attendance/team                 HR and PM: everybody below me on one day (?date=2026-11-12)
    GET  /attendance/person/{emp_id}      HR and PM: one person's month
    POST /attendance/corrections          ask for a missed check-in / check-out (within 7 days)
    GET  /attendance/corrections/mine     my correction requests
    GET  /attendance/corrections/inbox    HR and PM: waiting requests of the people below me
    POST /attendance/corrections/{id}/decision   approve or reject
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
from starlette.concurrency import run_in_threadpool

from app.api.limits import ALLOWED_DOCUMENT_TYPES, MAXIMUM_DOCUMENT_BYTES
from app.api.requests import (
    AttachDocumentRequest,
    AttendanceCorrectionRequest,
    ChangePasswordRequest,
    ChatRequest,
    CorrectionDecisionRequest,
    LeaveApplyRequest,
    SuperadminAttendanceRequest,
    SuperadminLeaveRequest,
    LeaveDecisionRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
)
from app.api.responses import (
    AttendanceCorrection,
    AttendanceMonthResponse,
    AttendanceTeamResponse,
    AttendanceTodayResponse,
    ChatHistoryMessage,
    ChatResponse,
    CredentialsResponse,
    LoginResponse,
    ProfileResponse,
)
from app.core.logs import setup_logging
from app.identity import auth
from app.hr.attendance import service as attendance_service
from app.hr.leave import service as leave_service
from app.hr.leave import store as leave_store
from app.hr.payroll import service as payroll_service
from app.hr.payroll import store as payroll_store
from app.files import storage as storage_store
from app.rag.answer.assistant import answer_chat_message
from app.chat import store as chat_store
from app.hr.people import service as people_service
from app.hr.superadmin import service as superadmin_service
from app.identity.auth import HR_LEVEL_ROLES, AuthError
from app.hr.attendance.service import AttendanceError
from app.hr.leave.service import LeaveError
from app.hr.payroll.service import PayrollError

setup_logging()                      # the API is an entry point too
logger = logging.getLogger(__name__)

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


@app.exception_handler(AttendanceError)
def handle_attendance_error(request, error):
    """A broken attendance rule (for example "you already checked in") becomes a normal JSON error."""
    return JSONResponse(status_code=error.status_code, content={"detail": error.message, "code": "attendance_rule"})


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
    if employee["role"] not in HR_LEVEL_ROLES:
        raise AuthError(403, "Only HR can do this.")
    return employee


def require_superadmin(employee: dict = Depends(get_ready_employee)):
    """Only the super admin may continue."""
    if employee["role"] != "superadmin":
        raise AuthError(403, "Only the super admin can do this.")
    return employee


MESSAGES_SHOWN_TO_THE_AI = 6      # the AI reads the last 3 questions and 3 answers


# ------------------------------------------------------------------ endpoints
@app.get("/health")
def health_check():
    """Tells a load balancer or a person that the server is running."""
    return {"status": "ok"}


@app.post("/login", response_model=LoginResponse)
def login(request: LoginRequest):
    """Employee id + password in, token out."""
    result = auth.authenticate(request.emp_id, request.password)
    # The website hides the attendance section for the CEO and the CTO, and shows the team pages to HR and the PM.
    return dict(result, attendance_access=attendance_service.get_access_level(result) or "none")


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
    return dict({k: employee[k] for k in ("emp_id", "full_name", "dept", "role", "must_change_password")},
                attendance_access=attendance_service.get_access_level(employee) or "none")


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


@app.get("/me/profile", response_model=ProfileResponse)
def read_my_profile(employee: dict = Depends(get_ready_employee)):
    """My own details. There is no way to ask for anybody else's here."""
    return people_service.read_own_profile(employee)


@app.get("/chat/history", response_model=list[ChatHistoryMessage])
def chat_history(limit: int = Query(chat_store.MESSAGES_KEPT_PER_EMPLOYEE, ge=1, le=100),
                 before_id: int | None = Query(None, ge=1),
                 employee: dict = Depends(get_ready_employee)):
    """My own saved chat messages, oldest first. The database keeps only the newest
    chat_store.MESSAGES_KEPT_PER_EMPLOYEE of them, so there is normally no older page."""
    return chat_store.list_messages(employee["employee_id"], limit, before_id)


# ------------------------------------------------------------------ attendance
def get_client_ip(request: Request):
    """The address the request came from. Behind a proxy (set TRUST_PROXY_HEADERS=true) it is the first X-Forwarded-For address."""
    if os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() == "true":
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else ""


@app.get("/attendance/today", response_model=AttendanceTodayResponse)
def attendance_today(employee: dict = Depends(get_ready_employee)):
    return attendance_service.get_today_state(employee)


@app.post("/attendance/check-in", response_model=AttendanceTodayResponse)
def attendance_check_in(request: Request, employee: dict = Depends(get_ready_employee)):
    return attendance_service.check_in(employee, get_client_ip(request))


@app.post("/attendance/check-out", response_model=AttendanceTodayResponse)
def attendance_check_out(request: Request, employee: dict = Depends(get_ready_employee)):
    return attendance_service.check_out(employee, get_client_ip(request))


@app.get("/attendance/mine", response_model=AttendanceMonthResponse)
def attendance_mine(month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"), employee: dict = Depends(get_ready_employee)):
    return attendance_service.get_my_month(employee, month)


@app.get("/attendance/team", response_model=AttendanceTeamResponse)
def attendance_team(date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"), employee: dict = Depends(get_ready_employee)):
    """HR and the PM: everybody below them on one day (today when no date is given)."""
    return attendance_service.get_team_day(employee, date)


@app.get("/attendance/person/{emp_id}", response_model=AttendanceMonthResponse)
def attendance_person(emp_id: str, month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
                      employee: dict = Depends(get_ready_employee)):
    return attendance_service.get_person_month(employee, emp_id, month)


@app.post("/attendance/corrections", response_model=AttendanceCorrection)
def attendance_ask_correction(request: AttendanceCorrectionRequest, employee: dict = Depends(get_ready_employee)):
    return attendance_service.create_correction(employee, request.work_date, request.check_in_time, request.check_out_time, request.reason)


@app.get("/attendance/corrections/mine", response_model=list[AttendanceCorrection])
def attendance_my_corrections(employee: dict = Depends(get_ready_employee)):
    return attendance_service.list_my_corrections(employee)


@app.get("/attendance/corrections/inbox", response_model=list[AttendanceCorrection])
def attendance_correction_inbox(employee: dict = Depends(get_ready_employee)):
    return attendance_service.list_correction_inbox(employee)


@app.post("/attendance/corrections/{correction_id}/decision", response_model=AttendanceCorrection)
def attendance_decide_correction(correction_id: int, request: CorrectionDecisionRequest, employee: dict = Depends(get_ready_employee)):
    decided = attendance_service.decide_correction(employee, correction_id, request.decision, request.note)
    logger.info("Attendance correction %s %s by %s", correction_id, request.decision, employee["emp_id"])
    return decided


@app.get("/celebrations")
def celebrations(employee: dict = Depends(get_ready_employee)):
    """Birthdays and work anniversaries of today (India date), for the home page."""
    return people_service.get_celebrations(leave_service.get_today())


# ------------------------------------------------------------------ super admin
@app.get("/superadmin/employees")
def superadmin_employees(admin: dict = Depends(require_superadmin)):
    return superadmin_service.list_employees(admin)


@app.get("/superadmin/leave-types")
def superadmin_leave_types(admin: dict = Depends(require_superadmin)):
    return superadmin_service.list_leave_types(admin)


@app.get("/superadmin/leave-balances/{emp_id}")
def superadmin_leave_balances(emp_id: str, on_date: datetime.date | None = None, admin: dict = Depends(require_superadmin)):
    return superadmin_service.get_leave_balances(admin, emp_id, on_date or leave_service.get_today())


@app.post("/superadmin/leave")
def superadmin_apply_leave(request: SuperadminLeaveRequest, admin: dict = Depends(require_superadmin)):
    result = superadmin_service.apply_leave(admin, request.emp_id, request.leave_type, request.start_date, request.end_date,
                                            request.day_type, request.reason)
    logger.info("Super admin %s applied leave %s for %s", admin["emp_id"], result["leave_id"], request.emp_id)
    return result


@app.post("/superadmin/attendance")
def superadmin_mark_attendance(request: SuperadminAttendanceRequest, admin: dict = Depends(require_superadmin)):
    result = superadmin_service.mark_attendance(admin, request.emp_id, request.work_date, request.check_in_time, request.check_out_time)
    logger.info("Super admin %s set attendance of %s on %s", admin["emp_id"], request.emp_id, request.work_date)
    return result


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
    approvers = leave_store.get_employees_by_ids([row["approver_id"] for row in rows if row.get("approver_id")])
    return [dict(row,
                 description=leave_service.describe_leave_row(row, leave_types),
                 decided_by_name=(approvers.get(row.get("approver_id")) or {}).get("full_name"))
            for row in rows]


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
    leave_service.refuse_superadmin(employee)
    result = leave_service.apply_on_website(
        load_employee_row(employee), 
        request.leave_type, 
        request.start_date, 
        request.end_date,
        request.day_type, 
        request.reason, 
        request.document_path)
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
