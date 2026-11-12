"""
assistant.py - decides what a chat message is about and answers it.

llm.py answers policy questions from the documents. This file sits in front of it:

    1. If a leave is waiting for "yes", a message of "yes" / "no" confirms or cancels it (plain word check, no AI).
    2. Otherwise a small Groq call with TOOLS picks one of (salary and payslips: see hr/payroll/service.py):
         search_policy      -> llm.answer_question (the documents only)
         get_leave_balance  -> the employee's numbers + the policy text, combined in one answer
         list_my_leaves     -> the employee's requests + the policy text
         apply_leave        -> checks the rules, then asks "reply yes to confirm" (nothing is saved yet)
         cancel_leave       -> same, asks for confirmation first
    3. The AI only PICKS the tool and reads the dates. The rules, the checks and the saving are normal code
       (hr/leave/service.py), and every tool works only on the logged-in employee's own data.
"""
import json
import re
import time

import requests

from app.hr.attendance import service as attendance_service
from app.hr.leave import service as leave_service
from app.hr.leave import store as leave_store
from app.hr.payroll import service as payroll_service
from app.hr.people import service as people_service
from app.hr.people import store as people_store
from app.core.config import get_required_setting
from app.hr.leave.service import LeaveError
from app.rag.answer.llm import ANSWER_MAX_TOKENS, ANSWER_MODEL_NAME, ANSWER_SYSTEM_PROMPT, CITATION_PATTERN, GROQ_API_URL, MAXIMUM_ATTEMPTS, NUMBER_OF_CHUNKS_FOR_ANSWER, REASONING_EFFORT, REQUEST_TIMEOUT_SECONDS, RETRY_STATUS_CODES, SECONDS_TO_WAIT_BEFORE_RETRY, answer_question, build_context, call_groq, clean_answer_text, find_cited_sources
from app.rag.answer.retriever import search_chunks

ROUTER_MAX_TOKENS = 800

CONFIRM_WORDS = {"yes", "y", "yes confirm", "confirm", "yep", "yeah", "yes please"}
CANCEL_WORDS = {"no", "n", "cancel", "nope", "no cancel", "no thanks"}

DOCUMENT_PROMPT = "Please upload related document(Optional)."
APPLY_EXAMPLE = "Please tell me the leave type and the date in one message, for example: Apply casual leave for 10 Nov 2026, full day."

ROUTER_TOOLS = [
    {"type": "function", "function": {
        "name": "search_policy",
        "description": "Answer a question about company rules, policies or procedures from the HR policy documents.",
        "parameters": {"type": "object", "properties": {
            "standalone_question": {"type": "string", "description": "The question rewritten so it makes sense alone, using earlier messages when the new one is a follow-up. Empty if it already makes sense alone."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_leave_balance",
        "description": "Leave numbers: allowed, used, waiting for approval and remaining days. Of the employee, or (HR only) of another person.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string", "description": "Empty for the employee themself. Otherwise the employee ID or name they asked about."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "list_my_leaves",
        "description": "Leave requests, for example the ones still pending approval. Of the employee, or (HR only) of another person.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "cancelled", "any"]},
            "person": {"type": "string", "description": "Empty for the employee themself. Otherwise the employee ID or name they asked about."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_employee_info",
        "description": "Everything about one employee: profile, department, contact details, salary, leave. Of the employee themself, or (HR only) of another person.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string", "description": "Empty for the employee themself. Otherwise the employee ID or name."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_attendance",
        "description": "Attendance of a month: present, half day, absent, late, early leave, hours worked, check-in and check-out times. Of the employee, or (HR and PM only) of another person.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "YYYY-MM. Empty string for the current month."},
            "person": {"type": "string", "description": "Empty for the employee themself. Otherwise the employee ID or name they asked about."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "apply_leave",
        "description": "The employee asks to take leave on certain dates.",
        "parameters": {"type": "object", "properties": {
            "leave_type": {"type": "string", "description": "casual, sick or maternity. Empty string if the employee did not say."},
            "start_date": {"type": "string", "description": "YYYY-MM-DD. Empty string if the employee gave no date."},
            "end_date": {"type": "string", "description": "YYYY-MM-DD. Same as start_date for a single day."},
            "day_type": {"type": "string", "enum": ["full", "half"]},
            "reason": {"type": "string", "description": "Only if the employee stated one. Otherwise an empty string."}},
            "required": ["leave_type", "start_date"]}}},
    {"type": "function", "function": {
        "name": "get_salary",
        "description": "A salary or salary breakdown question: the employee's own, or (HR only) another person's.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string", "description": "Empty for the employee's own salary. Otherwise the employee ID, the name, or CEO."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_payslip",
        "description": "The employee asks for their own payslip or a link to it.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "YYYY-MM. Empty string for the latest payslip."}}, "required": []}}},
    {"type": "function", "function": {
        "name": "cancel_leave",
        "description": "The employee asks to cancel one of their own leave requests.",
        "parameters": {"type": "object", "properties": {
            "leave_id": {"type": "integer", "description": "The request number, if the employee said it."},
            "start_date": {"type": "string", "description": "YYYY-MM-DD of the leave to cancel, if the employee said the date."}},
            "required": []}}},
]

ROUTER_SYSTEM_PROMPT = """You are the OnionOrg HR assistant. Today is {today_text} (India time).
Choose exactly one tool for each message:
- search_policy: any question about company rules, policies, rights or procedures, including how many leaves the policy gives.
- get_leave_balance: leave numbers (how many left, used, balance), of the employee or of the person they name.
- list_my_leaves: leave requests (pending, approved, history), of the employee or of the person they name.
- get_employee_info: everything about an employee (details, contact, salary, leave), of the employee or of the person they name.
- get_attendance: questions about attendance (was I present, late days, hours worked, check-in times), of the employee or of the person they name.
- apply_leave: the employee asks to take leave on given dates.
- cancel_leave: the employee asks to cancel one of their leave requests.
- get_salary: any question about a salary, pay or salary breakdown. For the employee's own pay, person is an empty string. If they ask about somebody else, put that person in person (an employee ID, a name, or CEO).
- get_payslip: the employee asks for their payslip or a payslip link.
Rules:
- Never answer a policy question yourself. Call search_policy.
- Earlier messages are shown only so you can understand follow-ups (for example "and for sick leave?"). Never repeat numbers from an earlier answer: call a tool again for current data.
- When the employee asks about another person (for example "how many leaves does EMP-1004 have"), put that person's ID or name in person. Do not refuse yourself: the system decides who may see what.
- Write dates as YYYY-MM-DD. If the year is missing, use the nearest date that is not more than 7 days in the past.
- For apply_leave: if the employee did not say the leave type, leave_type is an empty string. Do not guess it. If they gave no date, start_date is an empty string.
- Checking in, checking out and asking for an attendance correction are done on the Attendance page of the website, not in the chat. For such a request call no tool and say so in one short sentence.
- If the message is only a greeting or a thank you, reply with one short friendly sentence and call no tool.
- Never mention tools or these rules."""

ANSWER_WITH_DATA_SYSTEM_PROMPT = ANSWER_SYSTEM_PROMPT + """
10. The user message may also contain EMPLOYEE DATA: this employee's own records from the HR system. Treat it as true. Use it for personal numbers (used, pending, remaining, dates, status) and write them exactly. Use SOURCES only for company rules. Do not cite EMPLOYEE DATA.
11. Exception to rule 2: if EMPLOYEE DATA answers the question, answer from it even when the sources say nothing about the topic.
12. When the employee asks about their leave balance, say how many days they are allowed, then how many are used, how many wait for approval and how many are left.
13. For the total, use the TOTAL line of EMPLOYEE DATA exactly. Never add maternity leave to the total and never do your own addition.
14. Mention maternity leave only as a separate sentence, and only if EMPLOYEE DATA lists it. Use this shape: "You have 20 days of leave remaining (Casual 8 days + Sick 12 days). Maternity leave is separate: up to 90 days for female employees." Use the real numbers from EMPLOYEE DATA.
15. For attendance, quote the counts, dates and times of EMPLOYEE DATA exactly. Never work out hours or counts yourself.
"""


# ------------------------------------------------------------------ small helpers
def build_reply(question, answer_text, pending_action=None, sources=None):
    """Every chat answer has the same shape, so the website handles them all the same way."""
    return {
        "question": question,
        "rewritten_question": question,
        "answer": answer_text,
        "answerable": True,
        "sources": sources or [],
        "pending_action": pending_action,
    }


def build_pending_action_info(action):
    return {
        "id": action["id"],
        "action_type": action["action_type"],
        "summary": action["summary"],
        "allows_document": action["action_type"] == leave_service.APPLY_LEAVE_ACTION,
        "expires_at": action["expires_at"],
    }


def normalise_words(text):
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


# ------------------------------------------------------------------ talking to Groq with tools
MAXIMUM_HISTORY_MESSAGES = 6            # the last 3 questions and 3 answers are shown to the model
MAXIMUM_HISTORY_MESSAGE_LENGTH = 600    # characters kept of each earlier message


def build_history_messages(history):
    """Turn saved chat rows (oldest first) into the model's message format. Long messages are cut short."""
    messages = []
    for row in (history or [])[-MAXIMUM_HISTORY_MESSAGES:]:
        role = "user" if row["sender"] == "user" else "assistant"
        messages.append({"role": role, "content": str(row["message"])[:MAXIMUM_HISTORY_MESSAGE_LENGTH]})
    return messages


def call_router(question, history=None):
    """Ask the model which tool fits the message. Returns the model's message (it may hold "tool_calls" or plain "content")."""
    today = leave_service.get_today()
    system_prompt = ROUTER_SYSTEM_PROMPT.format(today_text=f"{today.strftime('%A')}, {today.isoformat()}")
    request_body = {
        "model": ANSWER_MODEL_NAME,
        "messages": [{"role": "system", "content": system_prompt}] + build_history_messages(history) + [{"role": "user", "content": question}],
        "tools": ROUTER_TOOLS,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": ROUTER_MAX_TOKENS,
    }
    if REASONING_EFFORT:
        request_body["reasoning_effort"] = REASONING_EFFORT
    headers = {"Authorization": f"Bearer {get_required_setting('GROQ_API_KEY')}", "Content-Type": "application/json"}

    for attempt_number in range(1, MAXIMUM_ATTEMPTS + 1):
        response = requests.post(GROQ_API_URL, headers=headers, json=request_body, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code in RETRY_STATUS_CODES and attempt_number < MAXIMUM_ATTEMPTS:
            time.sleep(SECONDS_TO_WAIT_BEFORE_RETRY)
            continue
        if not response.ok:
            raise RuntimeError(f"Groq request failed ({response.status_code}): {response.text[:500]}")
        return response.json()["choices"][0]["message"]


def answer_with_employee_data(question, employee_data_text):
    """One answer that mixes the employee's own numbers with the policy documents."""
    try:
        chunks = search_chunks(question, NUMBER_OF_CHUNKS_FOR_ANSWER)
    except Exception:
        chunks = []          # the employee's own data is still worth showing if the document search fails
    user_message = f"EMPLOYEE DATA:\n{employee_data_text}\n\nSOURCES:\n{build_context(chunks)}\n\nQUESTION: {question}"
    answer_text = clean_answer_text(call_groq(
        ANSWER_MODEL_NAME,
        [{"role": "system", "content": ANSWER_WITH_DATA_SYSTEM_PROMPT}, {"role": "user", "content": user_message}],
        max_tokens=ANSWER_MAX_TOKENS,
    ))
    has_citations = bool(CITATION_PATTERN.search(answer_text))
    return build_reply(question, answer_text, sources=find_cited_sources(answer_text, chunks) if has_citations else [])


# ------------------------------------------------------------------ employee data as plain text
def build_balance_text(employee_row, owner_label=None):
    today = leave_service.get_today()
    lines = [f"Today is {today.isoformat()}. The leave year is {leave_service.leave_year_label(today)} (1 April to 31 March)."]
    if owner_label:
        lines.append(f"These records belong to {owner_label}, not to the person asking (the person asking is HR).")
    balances = leave_store.get_leave_balances(employee_row["id"], today)
    yearly_balances = [balance for balance in balances if balance["limit_type"] == "per_year"]
    # The total is worked out here, in code, so the AI never has to add numbers (and never adds maternity leave to it).
    total_left = sum(float(balance["remaining_days"]) for balance in yearly_balances)
    total_allowed = sum(float(balance["days_allowed"]) for balance in yearly_balances)
    names = " + ".join(balance["type_name"] for balance in yearly_balances)
    lines.append(f"TOTAL leave days left: {total_left:g} of {total_allowed:g} ({names}). Maternity leave is never part of this total.")
    for balance in yearly_balances:
        lines.append(f"{balance['type_name']}: {float(balance['days_allowed']):g} days per year, used {float(balance['used_days']):g}, "
                     f"waiting for approval {float(balance['pending_days']):g}, remaining {float(balance['remaining_days']):g}.")
    for balance in balances:
        if balance["limit_type"] != "per_year":
            lines.append(f"{balance['type_name']} (separate, not part of the total): up to {float(balance['days_allowed']):g} days "
                         "for female employees, applied on the website with a document.")
    return "\n".join(lines)


def build_leave_list_text(employee_row, status, owner_label=None):
    leave_types = leave_store.get_leave_types()
    rows = leave_store.list_leave_requests(employee_row["id"], None if status in (None, "any") else status)
    owner_line = f"These records belong to {owner_label}, not to the person asking (the person asking is HR).\n" if owner_label else ""
    if not rows:
        return owner_line + f"No {'' if status in (None, 'any') else status + ' '}leave requests found."
    return owner_line + "Leave requests (newest first):\n" + "\n".join(
        f"#{row['id']} {leave_service.describe_leave_row(row, leave_types)} - status: {row['status']}" for row in rows)


# ------------------------------------------------------------------ the five tools
def handle_apply_leave(employee_row, question, arguments):
    leave_type_code = (arguments.get("leave_type") or "").strip()
    start_text = (arguments.get("start_date") or "").strip()
    if not leave_type_code or not start_text:
        return build_reply(question, APPLY_EXAMPLE)

    start_date = leave_service.parse_date(start_text, "start date")
    end_date = leave_service.parse_date(arguments.get("end_date") or start_text, "end date")
    day_type = arguments.get("day_type") or "full"
    details = leave_service.check_new_leave(employee_row, leave_type_code, start_date, end_date, day_type, applied_by_chat=True)

    reason = (arguments.get("reason") or "").strip() or None
    summary = leave_service.describe_leave(details["leave_type_name"], start_date, end_date, day_type, details["days"])
    action = leave_store.create_pending_action(
        employee_row["id"], leave_service.APPLY_LEAVE_ACTION, dict(details, reason=reason), summary)
    answer_text = f"{summary}.\n{DOCUMENT_PROMPT}\nReply yes to confirm, or no to cancel."
    return build_reply(question, answer_text, pending_action=build_pending_action_info(action))


def handle_cancel_leave(employee_row, question, arguments):
    leave_id = arguments.get("leave_id")
    if not leave_id and arguments.get("start_date"):
        wanted_date = leave_service.parse_date(arguments["start_date"], "date").isoformat()
        matches = [row for row in leave_store.list_leave_requests(employee_row["id"])
                   if row["start_date"] == wanted_date and row["status"] in ("pending", "approved")]
        if len(matches) == 1:
            leave_id = matches[0]["id"]
    if not leave_id:
        return build_reply(question, "Which leave do you want to cancel? Please tell me the request number, for example: Cancel leave #12.")

    leave_row = leave_service.check_cancellable(employee_row, int(leave_id))
    leave_types = leave_store.get_leave_types()
    summary = "Cancel " + leave_service.describe_leave_row(leave_row, leave_types)
    action = leave_store.create_pending_action(employee_row["id"], leave_service.CANCEL_LEAVE_ACTION, {"leave_id": leave_row["id"]}, summary)
    return build_reply(question, f"{summary}.\nReply yes to confirm, or no to keep it.", pending_action=build_pending_action_info(action))


# ------------------------------------------------------------------ "yes" and "no"
def handle_confirmation_words(employee_row, question, action):
    """Returns a reply when the message is yes or no, otherwise None (the message is handled as a normal question)."""
    words = normalise_words(question)
    if words not in CONFIRM_WORDS and words not in CANCEL_WORDS:
        return None

    if words in CANCEL_WORDS:
        leave_store.update_action(action["id"], {"status": "cancelled"})
        return build_reply(question, "Okay, I cancelled that. Nothing was changed.")

    if leave_service.is_expired(action):
        leave_store.update_action(action["id"], {"status": "expired"})
        return build_reply(question, "That request expired because it was not confirmed in time. Please send it again.")

    # Mark it confirmed first, so a double click on "yes" cannot do the action twice.
    if leave_store.update_action(action["id"], {"status": "confirmed"}) is None:
        return build_reply(question, "That request was already handled.")
    return build_reply(question, leave_service.run_confirmed_action(employee_row, action))


# ------------------------------------------------------------------ the entry point used by /chat
def answer_chat_message(employee, question, history=None):
    """employee is the logged-in employee dictionary from the token. history is the saved earlier messages (oldest first).
    Returns the dictionary /chat sends back."""
    clean_question = question.strip()
    if not clean_question:
        raise ValueError("The question is empty.")

    employee_row = leave_store.get_employee_row(employee["employee_id"])
    try:
        waiting_action = leave_store.get_waiting_action(employee["employee_id"])
        if waiting_action:
            confirmation_reply = handle_confirmation_words(employee_row, clean_question, waiting_action)
            if confirmation_reply:
                return confirmation_reply

        message = call_router(clean_question, history)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            reply_text = (message.get("content") or "").strip()
            if reply_text:
                return build_reply(clean_question, reply_text)
            return dict(answer_question(clean_question), pending_action=None)       # the model said nothing: use the documents

        tool_name = tool_calls[0]["function"]["name"]
        try:
            arguments = json.loads(tool_calls[0]["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}

        if tool_name in ("get_leave_balance", "list_my_leaves", "get_employee_info"):
            # Who may see whom is decided here, in code, before any data is read. None means "the person asking".
            target = people_service.resolve_target(employee, arguments.get("person"))
            subject_row = target or employee_row
            owner_label = f"{target['full_name']} ({target['emp_id']})" if target else None
            if tool_name == "get_employee_info":
                return build_reply(clean_question, people_service.build_profile_text(target or people_store.get_profile(employee["employee_id"])))
            if tool_name == "get_leave_balance":
                return answer_with_employee_data(clean_question, build_balance_text(subject_row, owner_label))
            return answer_with_employee_data(clean_question, build_leave_list_text(subject_row, arguments.get("status"), owner_label))
        if tool_name == "get_attendance":
            attendance_text = attendance_service.build_chat_text(employee, arguments.get("person"), (arguments.get("month") or "").strip() or None)
            return answer_with_employee_data(clean_question, attendance_text)
        if tool_name == "apply_leave":
            leave_service.refuse_superadmin(employee)
            return handle_apply_leave(employee_row, clean_question, arguments)
        if tool_name == "cancel_leave":
            return handle_cancel_leave(employee_row, clean_question, arguments)
        if tool_name == "get_salary":
            return build_reply(clean_question, payroll_service.answer_salary_question(employee, arguments.get("person")))
        if tool_name == "get_payslip":
            return build_reply(clean_question, payroll_service.answer_payslip_question(employee, arguments.get("month")))
        policy_question = (arguments.get("standalone_question") or "").strip() or clean_question       # a follow-up becomes a full question
        return dict(answer_question(policy_question), question=clean_question, pending_action=None)     # search_policy or an unknown tool
    except (LeaveError, payroll_service.PayrollError, people_service.PeopleError, attendance_service.AttendanceError) as error:
        # A broken rule is a normal answer ("You have only 3 days left"), not a server error.
        return build_reply(clean_question, error.message)
