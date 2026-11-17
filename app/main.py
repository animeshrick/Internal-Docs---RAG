"""
FastAPI app for the HR policy chatbot.

Run from the project root:
    uvicorn app.main:app --reload

The request and response shapes live in app/api/requests.py and app/api/responses.py;
the input size limits in app/api/limits.py.

Then open http://127.0.0.1:8000/docs to try it in the browser.

Endpoints:
    GET  /health                          open to everyone
    GET  /system/downtime                 open to everyone: which pages are down for planned maintenance
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
    GET  /payroll/payslips/mine/years     every year from my joining year to the current year
    GET  /payroll/payslips/mine           January-December of one year (default: current year), whether a payslip exists for each month
    GET  /payroll/payslips/mine/{month}/download-url   my own payslip for one month (generated the first time it's asked for)
    POST /admin/employees                 HR only: add a new employee row (before creating their login)
    POST /register                        HR only: create a login for an existing employee
    POST /reset-password                  HR only: give an employee a new temporary password
    GET  /superadmin/audit-log            super admin only: who was acted on, what, and when
    GET  /superadmin/whatsapp-numbers     super admin only: the numbers receiving the twice-daily report
    POST /superadmin/whatsapp-numbers     super admin only: add a number (label + phone number), max 10
    DELETE /superadmin/whatsapp-numbers/{id}  super admin only: remove a number
    POST /superadmin/whatsapp-report/send-now  super admin only: send today's report right now (to test AiSensy)
    GET  /admin/attendance-settings       HR only: the network check switch and the office networks
    POST /admin/attendance-settings/network-check   HR only: turn the office-network restriction on or off
    POST /admin/office-networks           HR only: add an office network (label + CIDR)
    POST /admin/office-networks/{id}/active         HR only: turn one office network on or off
    POST /documents                       upload a shared document (raw file in the body); you become its owner
    GET  /documents                       everyone's file names, with my own access state on each
    GET  /documents/{id}                  one document's details (the owner also sees its request list)
    POST /documents/{id}/request          ask the owner for read-only access to a document that is not mine
    GET  /documents/requests/inbox        requests waiting for a decision, across documents I own
    GET  /documents/requests/mine         what happened to every access request I made
    POST /documents/requests/{id}/decision   the owner approves or denies a request (10 days, view-only)
    GET  /documents/{id}/download-url     temporary link to the file itself (owner, or active approved access)
    DELETE /documents/{id}                owner only: delete the document and its stored file
    GET  /messages/people                 everyone I may message (open: anyone, including HR/CEO/super admin)
    GET  /messages/conversations          my conversations from the last 24 hours, newest first, with unread counts
    GET  /messages/thread/{emp_id}         my messages with one person from the last 24 hours (marks it read)
    POST /messages/send                   send a message (text and/or an attachment) to one person
    POST /messages/attachments            upload a PDF / PNG / JPG to attach to a message (raw file in the body)

There is no public sign-up. HR creates every login.
"""

import datetime
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.limits import (
    ALLOWED_DOCUMENT_TYPES,
    ALLOWED_MESSAGE_ATTACHMENT_TYPES,
    ALLOWED_SHARED_DOCUMENT_TYPES,
    MAXIMUM_DOCUMENT_BYTES,
    MAXIMUM_MESSAGE_ATTACHMENT_BYTES,
    MAXIMUM_SHARED_DOCUMENT_BYTES,
)
from app.api.requests import (
    AttachDocumentRequest,
    AttendanceCorrectionRequest,
    ChangePasswordRequest,
    ChatRequest,
    CorrectionDecisionRequest,
    CreateEmployeeRequest,
    DocumentAccessDecisionRequest,
    LeaveApplyRequest,
    NetworkCheckRequest,
    OfficeNetworkActiveRequest,
    OfficeNetworkRequest,
    SuperadminAttendanceRequest,
    SuperadminLeaveRequest,
    LeaveDecisionRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SendMessageRequest,
    WhatsAppNumberRequest,
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
from app.core import downtime
from app.core import presence
from app.core.logs import setup_logging
from app.identity import auth
from app.hr.attendance import service as attendance_service
from app.hr.documents import service as documents_service
from app.hr.documents import store as documents_store
from app.hr.employees import service as employees_service
from app.hr.leave import service as leave_service
from app.hr.leave import store as leave_store
from app.hr.messages import service as messages_service
from app.hr.payroll import service as payroll_service
from app.hr.payroll import store as payroll_store
from app.hr.reports import service as reports_service
from app.hr.reports import scheduler as reports_scheduler
from app.hr.attendance import checkout_reminder
from app.files import storage as storage_store
from app.rag.answer.assistant import answer_chat_message
from app.chat import store as chat_store
from app.hr.people import service as people_service
from app.hr.superadmin import service as superadmin_service
from app.identity.auth import HR_LEVEL_ROLES, AuthError
from app.hr.attendance.service import AttendanceError
from app.hr.leave.service import LeaveError
from app.hr.payroll.service import PayrollError
from app.hr.reports.service import ReportError

setup_logging()                      # the API is an entry point too
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reports_scheduler.start()
    checkout_reminder.start()
    yield
    reports_scheduler.stop()
    checkout_reminder.stop()


app = FastAPI(title="HR Policy Chatbot", lifespan=lifespan)


async def _downtime_middleware(request: Request, call_next):
    """Refuses a request for a page that DOWNTIME_PAGES currently marks down, before it reaches
    any route. See app/core/downtime.py for the page keys and which routes belong to each."""
    page_key = downtime.page_key_for_path(request.url.path)
    if page_key and page_key in downtime.get_down_pages():
        return JSONResponse(status_code=503, content={"detail": downtime.DOWNTIME_MESSAGE, "code": "page_down", "page": page_key})
    return await call_next(request)


app.add_middleware(BaseHTTPMiddleware, dispatch=_downtime_middleware)

# The website is normally served from the same address (through a proxy), so CORS stays off by default.
# If the website runs on its own address, list it in .env, for example:
#   CORS_ALLOWED_ORIGINS=https://hr.example.com,http://localhost:5173
allowed_origins = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
if allowed_origins:
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["GET", "POST", "DELETE"],
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


@app.exception_handler(ReportError)
def handle_report_error(request, error):
    """A broken WhatsApp-report rule (for example "already 10 numbers") becomes a normal JSON error."""
    return JSONResponse(status_code=error.status_code, content={"detail": error.message, "code": "report_rule"})


# ------------------------------------------------------------------ who is calling?
def get_current_employee(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """Any valid login token. Used by /change-password."""
    if credentials is None:
        raise AuthError(401, "Please log in first.")
    employee = auth.load_employee_from_token(credentials.credentials)
    presence.mark_active(employee["employee_id"])     # the green "online" dot in Messages reads this
    return employee


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


@app.get("/system/downtime")
def system_downtime_status():
    """Public, no login needed: which pages are down for planned maintenance right now (see
    DOWNTIME_PAGES in app/core/downtime.py). The frontend polls this to show a maintenance
    screen on the matching pages instead of waiting for each of their requests to fail."""
    return {"down_pages": sorted(downtime.get_down_pages())}


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


@app.post("/admin/employees")
def create_employee(request: CreateEmployeeRequest, hr_employee: dict = Depends(require_hr)):
    """HR only. Adds a new employee row. Call /register afterwards to give them a login."""
    result = employees_service.create_employee(request.model_dump())
    logger.info("Employee %s created by %s", result["emp_id"], hr_employee["emp_id"])
    return result


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


@app.get("/superadmin/audit-log")
def superadmin_audit_log_endpoint(limit: int = Query(50, ge=1, le=200), admin: dict = Depends(require_superadmin)):
    """Who the super admin acted on, what was done, and when. Newest first."""
    return superadmin_service.list_audit_log(admin, limit)


# ------------------------------------------------------------------ WhatsApp daily report (super admin)
@app.get("/superadmin/whatsapp-numbers")
def list_whatsapp_numbers(admin: dict = Depends(require_superadmin)):
    """Every number that receives the twice-daily report (11am and 5pm India time), oldest first."""
    return reports_service.list_numbers(admin)


@app.post("/superadmin/whatsapp-numbers")
def add_whatsapp_number(request: WhatsAppNumberRequest, admin: dict = Depends(require_superadmin)):
    """Add one number. Up to 10 at a time; remove one first to add another."""
    result = reports_service.add_number(admin, request.label, request.phone_number)
    logger.info("WhatsApp report number %s added by %s", result["phone_number"], admin["emp_id"])
    return result


@app.delete("/superadmin/whatsapp-numbers/{number_id}")
def remove_whatsapp_number(number_id: int, admin: dict = Depends(require_superadmin)):
    result = reports_service.remove_number(admin, number_id)
    logger.info("WhatsApp report number %s removed by %s", result["phone_number"], admin["emp_id"])
    return {"message": "The number was removed."}


@app.post("/superadmin/whatsapp-report/send-now")
def send_whatsapp_report_now(admin: dict = Depends(require_superadmin)):
    """Builds and sends today's report right away, to every configured number - mainly to test
    the AiSensy setup without waiting for 11am or 5pm. One result per number."""
    return reports_service.send_daily_report_now(admin)


# ------------------------------------------------------------------ attendance admin (HR: office network)
@app.get("/admin/attendance-settings")
def admin_attendance_settings(hr_employee: dict = Depends(require_hr)):
    return attendance_service.get_admin_settings(hr_employee)


@app.post("/admin/attendance-settings/network-check")
def admin_set_network_check(request: NetworkCheckRequest, hr_employee: dict = Depends(require_hr)):
    result = attendance_service.set_network_check(hr_employee, request.enabled)
    logger.info("Network check set to %s by %s", request.enabled, hr_employee["emp_id"])
    return result


@app.post("/admin/office-networks")
def admin_add_office_network(request: OfficeNetworkRequest, hr_employee: dict = Depends(require_hr)):
    result = attendance_service.add_office_network(hr_employee, request.label, request.cidr)
    logger.info("Office network %s (%s) added by %s", result["label"], result["cidr"], hr_employee["emp_id"])
    return result


@app.post("/admin/office-networks/{network_id}/active")
def admin_set_office_network_active(network_id: int, request: OfficeNetworkActiveRequest, hr_employee: dict = Depends(require_hr)):
    result = attendance_service.set_office_network_active(hr_employee, network_id, request.is_active)
    logger.info("Office network %s set active=%s by %s", network_id, request.is_active, hr_employee["emp_id"])
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
    """Leaves waiting for this person to decide. Only the super admin decides any leave, so everyone
    else (including HR) gets an empty list here."""
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


@app.get("/payroll/payslips/mine/years")
def my_payslip_years(employee: dict = Depends(get_ready_employee)):
    """Every year from my joining year to the current year, newest first."""
    return payroll_service.list_my_payslip_years(employee)


@app.get("/payroll/payslips/mine")
def my_payslip_months(year: int | None = Query(default=None, ge=2000, le=2100), employee: dict = Depends(get_ready_employee)):
    """January-December of one year (the current year when none is given). Each month is marked with
    whether a payslip already exists, and - if it falls before I joined or after the current month -
    why it is disabled."""
    return payroll_service.list_my_payslip_months(employee, year)


@app.get("/payroll/payslips/mine/{month}/download-url")
def my_payslip_download_url(month: str, employee: dict = Depends(get_ready_employee)):
    """A temporary link to my own payslip for one month (format: 2026-10). Generated on first request."""
    return payroll_service.get_or_build_payslip_link(employee, month)


# ------------------------------------------------------------------ shared documents (everyone: upload, list, request, decide)
def make_safe_document_filename(filename):
    """Same rule as leave documents: letters, digits, dot, dash and underscore only."""
    return make_safe_filename(filename)


@app.post("/documents")
async def upload_shared_document(request: Request, filename: str = Query(min_length=1, max_length=120),
                                 employee: dict = Depends(get_ready_employee)):
    """Send the raw file as the body. Anyone may upload; the uploader becomes the owner.
    Content-Type must be one of ALLOWED_SHARED_DOCUMENT_TYPES (PDF, PNG, JPG, Word, Excel, PowerPoint)."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_SHARED_DOCUMENT_TYPES:
        raise AuthError(415, "That file type is not allowed. Use PDF, Word, Excel, PowerPoint, PNG or JPG.")
    declared_size = request.headers.get("content-length", "")
    if declared_size.isdigit() and int(declared_size) > MAXIMUM_SHARED_DOCUMENT_BYTES:
        raise AuthError(413, "The file is too large. The limit is 20 MB.")

    file_bytes = await request.body()
    if not file_bytes:
        raise AuthError(400, "The file is empty.")
    if len(file_bytes) > MAXIMUM_SHARED_DOCUMENT_BYTES:
        raise AuthError(413, "The file is too large. The limit is 20 MB.")

    safe_name = make_safe_document_filename(filename)
    storage_path = f"{employee['employee_id']}/{uuid.uuid4().hex}-{safe_name}"
    try:
        await run_in_threadpool(storage_store.upload_file, storage_store.SHARED_DOCUMENTS_BUCKET, storage_path, file_bytes, content_type)
    except Exception:
        logger.exception("Shared document upload failed")
        raise HTTPException(status_code=502, detail="The document could not be saved right now. Please try again.")

    saved = await run_in_threadpool(documents_store.insert_document, {
        "owner_id": employee["employee_id"], "original_filename": filename, "storage_path": storage_path,
        "content_type": content_type, "size_bytes": len(file_bytes),
    })
    logger.info("Shared document %s uploaded by %s", saved["id"], employee["emp_id"])
    return {"id": saved["id"], "original_filename": saved["original_filename"], "message": "Document uploaded."}


@app.get("/documents")
def list_shared_documents(employee: dict = Depends(get_ready_employee)):
    """Everyone's file names, in one list, with my own access state on each."""
    return documents_service.list_documents(employee)


@app.get("/documents/requests/inbox")
def shared_document_requests_inbox(employee: dict = Depends(get_ready_employee)):
    """Requests waiting for a decision, across every document I own."""
    return documents_service.list_my_pending_inbox(employee)


@app.get("/documents/requests/mine")
def my_shared_document_requests(employee: dict = Depends(get_ready_employee)):
    """What happened to every access request I made."""
    return documents_service.list_my_requests(employee)


@app.get("/documents/{document_id}")
def shared_document_detail(document_id: int, employee: dict = Depends(get_ready_employee)):
    """One document's details. The owner also sees the full list of requests for it."""
    return documents_service.get_document_detail(employee, document_id)


@app.post("/documents/{document_id}/request")
def request_shared_document_access(document_id: int, employee: dict = Depends(get_ready_employee)):
    """Ask the owner for read-only access to a document that is not mine."""
    documents_service.request_access(employee, document_id)
    return {"message": "Your request was sent to the owner."}


@app.post("/documents/requests/{request_id}/decision")
def decide_shared_document_request(request_id: int, request: DocumentAccessDecisionRequest, employee: dict = Depends(get_ready_employee)):
    """The owner approves or denies one request waiting for them."""
    updated = documents_service.decide_request(employee, request_id, request.decision)
    return {"id": updated["id"], "status": updated["status"], "expires_at": updated.get("expires_at")}


@app.get("/documents/{document_id}/download-url")
def shared_document_download_url(document_id: int, employee: dict = Depends(get_ready_employee)):
    """A temporary link to the file itself, read-only. Works for the owner, or anyone with active approved access."""
    storage_path = documents_service.check_can_read(employee, document_id)
    return {"url": storage_store.create_signed_link(storage_store.SHARED_DOCUMENTS_BUCKET, storage_path),
            "valid_for_seconds": storage_store.SIGNED_LINK_SECONDS}


@app.delete("/documents/{document_id}")
async def delete_shared_document(document_id: int, employee: dict = Depends(get_ready_employee)):
    """Only the owner may delete their own document. Removes the stored file and every access request for it."""
    document = documents_service.check_can_delete(employee, document_id)
    try:
        await run_in_threadpool(storage_store.delete_file, storage_store.SHARED_DOCUMENTS_BUCKET, document["storage_path"])
    except Exception:
        logger.exception("Could not delete the shared document's stored file (the record is still removed)")
    await run_in_threadpool(documents_store.delete_document, document_id)
    logger.info("Shared document %s deleted by %s", document_id, employee["emp_id"])
    return {"message": "Document deleted."}


# ------------------------------------------------------------------ 1-to-1 messages (open: anyone may message anyone)
@app.get("/messages/people")
def messages_people(employee: dict = Depends(get_ready_employee)):
    """Everyone I may start a conversation with. Nothing to approve - any employee may message any
    other employee, including HR, the CEO and the super admin."""
    return messages_service.list_people(employee)


@app.get("/messages/conversations")
def messages_conversations(employee: dict = Depends(get_ready_employee)):
    """My conversations with at least one message in the last 24 hours, newest first, each with
    an unread count. Older conversations are not deleted - they simply age out of this list."""
    return messages_service.list_conversations(employee)


@app.get("/messages/thread/{other_emp_id}")
def messages_thread(other_emp_id: str, employee: dict = Depends(get_ready_employee)):
    """My messages with one person from the last 24 hours, oldest first. Opening the thread marks
    it read, which is what clears that conversation's unread badge."""
    return messages_service.get_thread(employee, other_emp_id)


@app.post("/messages/send")
def messages_send(request: SendMessageRequest, employee: dict = Depends(get_ready_employee)):
    attachment = None
    if request.attachment_path:
        attachment = {
            "path": request.attachment_path,
            "filename": request.attachment_filename or request.attachment_path.rsplit("/", 1)[-1],
            "content_type": request.attachment_content_type or "application/octet-stream",
        }
    saved = messages_service.send_message(employee, request.to_emp_id, request.body, attachment)
    return saved


# async because the file arrives in the request body; the blocking upload runs in a worker thread.
@app.post("/messages/attachments")
async def upload_message_attachment(request: Request, filename: str = Query(min_length=1, max_length=120),
                                    employee: dict = Depends(get_ready_employee)):
    """Send the raw file as the body, with Content-Type application/pdf, image/png or image/jpeg.
    Returns the path to pass as attachment_path on POST /messages/send - the file is not attached
    to any message, and nobody else can read it, until that call is made."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_MESSAGE_ATTACHMENT_TYPES:
        raise AuthError(415, "Only PDF, PNG or JPG files can be attached.")
    declared_size = request.headers.get("content-length", "")
    if declared_size.isdigit() and int(declared_size) > MAXIMUM_MESSAGE_ATTACHMENT_BYTES:
        raise AuthError(413, "The file is too large. The limit is 5 MB.")

    file_bytes = await request.body()
    if not file_bytes:
        raise AuthError(400, "The file is empty.")
    if len(file_bytes) > MAXIMUM_MESSAGE_ATTACHMENT_BYTES:
        raise AuthError(413, "The file is too large. The limit is 5 MB.")

    safe_name = make_safe_filename(filename)
    attachment_path = f"{employee['employee_id']}/{uuid.uuid4().hex}-{safe_name}"
    try:
        await run_in_threadpool(storage_store.upload_file, storage_store.DIRECT_MESSAGE_ATTACHMENTS_BUCKET, attachment_path, file_bytes, content_type)
    except Exception:
        logger.exception("Message attachment upload failed")
        raise HTTPException(status_code=502, detail="The file could not be saved right now. Please try again.")
    return {"attachment_path": attachment_path, "attachment_filename": filename, "attachment_content_type": content_type}


@app.get("/messages/attachments/{message_id}/download-url")
def message_attachment_download_url(message_id: int, employee: dict = Depends(get_ready_employee)):
    """A temporary link to one message's attachment. Works for the sender or the recipient, as
    long as the message is still within the 24-hour visibility window."""
    attachment_path = messages_service.check_can_read_attachment(employee, message_id)
    return {"url": storage_store.create_signed_link(storage_store.DIRECT_MESSAGE_ATTACHMENTS_BUCKET, attachment_path),
            "valid_for_seconds": storage_store.SIGNED_LINK_SECONDS}
