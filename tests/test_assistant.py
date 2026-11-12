"""Tests for app/assistant.py. Groq (router and answer) and the document search are replaced by fakes."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.rag.answer import assistant as assistant
from app.hr.leave import service as leave_service
from fakes import FakeDatabase

TODAY = date(2026, 11, 7)
EMPLOYEE = {"employee_id": 3, "emp_id": "EMP003", "role": "employee"}


def tool_message(name, **arguments):
    return {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}]}


@pytest.fixture
def chat(monkeypatch):
    """Returns a function: chat("message", router_reply) sends one message with the router answering router_reply."""
    monkeypatch.setattr(leave_service, "get_today", lambda: TODAY)
    database = FakeDatabase().install(monkeypatch)
    state = {"router_reply": {"content": "Hello!"}, "groq_messages": []}

    monkeypatch.setattr(assistant, "call_router", lambda question, history=None: state["router_reply"])
    monkeypatch.setattr(assistant, "answer_question", lambda question: {
        "question": question, "rewritten_question": question, "answer": "Policy answer [1].", "answerable": True,
        "sources": [{"number": 1, "chunk_id": "c1", "doc_id": "HR-LEAVE-001"}]})
    monkeypatch.setattr(assistant, "search_chunks", lambda question, count: [{
        "chunk_id": "c1", "doc_id": "HR-LEAVE-001", "title": "Leave Policy", "section": "2", "version": "1", "effective_date": None,
        "text": "Casual leave is 8 days per year."}])

    def fake_call_groq(model, messages, max_tokens, temperature=0.0):
        state["groq_messages"].append(messages[-1]["content"])
        return "You have 3 casual days left out of 8 [1]."
    monkeypatch.setattr(assistant, "call_groq", fake_call_groq)

    def send(message, router_reply=None, employee=EMPLOYEE):
        if router_reply is not None:
            state["router_reply"] = router_reply
        return assistant.answer_chat_message(employee, message)

    send.database = database
    send.state = state
    return send


def test_a_policy_question_uses_the_documents_only(chat):
    reply = chat("How many casual leaves?", tool_message("search_policy"))
    assert reply["answer"] == "Policy answer [1]."
    assert reply["pending_action"] is None


def test_a_greeting_gets_the_models_short_reply(chat):
    reply = chat("hey", {"content": "Hello! How can I help?"})
    assert reply["answer"] == "Hello! How can I help?"
    assert reply["sources"] == []


def test_the_balance_mixes_my_numbers_with_the_policy(chat):
    chat.database.add_leave(3, "casual", "2026-06-01", "2026-06-04", 4, status="approved")
    chat.database.add_leave(3, "casual", "2026-04-06", "2026-04-06", 1, status="pending")
    reply = chat("how many casual leaves do I have left?", tool_message("get_leave_balance"))
    data_sent_to_the_model = chat.state["groq_messages"][-1]
    assert "Casual Leave: 8 days per year, used 4, waiting for approval 1, remaining 3." in data_sent_to_the_model
    assert "TOTAL leave days left: 15 of 20 (Casual Leave + Sick Leave). Maternity leave is never part of this total." in data_sent_to_the_model
    assert "Maternity Leave (separate, not part of the total): up to 90 days for female employees" in data_sent_to_the_model
    assert "110" not in data_sent_to_the_model
    assert "Casual leave is 8 days per year." in data_sent_to_the_model                 # the policy text is there too
    assert reply["sources"][0]["doc_id"] == "HR-LEAVE-001"                                # cited, so it is shown


def test_my_pending_leaves_are_listed(chat):
    chat.database.add_leave(3, "sick", "2026-11-12", "2026-11-12", 1, status="pending")
    chat("which leaves are pending?", tool_message("list_my_leaves", status="pending"))
    assert "Sick Leave on Thu, 12 Nov 2026 (full day) - status: pending" in chat.state["groq_messages"][-1]


def test_the_answer_still_works_when_the_document_search_fails(chat, monkeypatch):
    def broken_search(question, count):
        raise RuntimeError("embedding service down")
    monkeypatch.setattr(assistant, "search_chunks", broken_search)
    reply = chat("my balance", tool_message("get_leave_balance"))
    assert "3 casual days" in reply["answer"]


# ------------------------------------------------------------------ apply with "reply yes"
APPLY = dict(leave_type="casual", start_date="2026-11-10", end_date="2026-11-10", day_type="full")


def test_apply_asks_for_confirmation_and_saves_nothing_yet(chat):
    reply = chat("Apply leave for 10th Nov, 2026 for full day", tool_message("apply_leave", **APPLY))
    assert "Casual Leave on Tue, 10 Nov 2026 (full day)" in reply["answer"]
    assert "Please upload related document(Optional)." in reply["answer"]
    assert "Reply yes to confirm" in reply["answer"]
    assert reply["pending_action"]["allows_document"] is True
    assert chat.database.leave_requests == []


def test_yes_saves_the_leave_and_names_the_approver(chat):
    chat("Apply leave for 10th Nov, 2026", tool_message("apply_leave", **APPLY))
    reply = chat("Yes", {"content": "should not be asked"})
    assert "sent to Rahul Verma for approval" in reply["answer"]
    assert len(chat.database.leave_requests) == 1
    assert chat.database.leave_requests[0]["status"] == "pending"


def test_a_second_yes_does_not_apply_twice(chat):
    chat("apply", tool_message("apply_leave", **APPLY))
    chat("yes")
    chat("yes", {"content": "Nothing to confirm."})
    assert len(chat.database.leave_requests) == 1


def test_no_cancels_the_waiting_request(chat):
    chat("apply", tool_message("apply_leave", **APPLY))
    reply = chat("no")
    assert "Nothing was changed" in reply["answer"]
    assert chat.database.leave_requests == []


def test_an_expired_request_is_not_applied(chat):
    chat("apply", tool_message("apply_leave", **APPLY))
    chat.database.actions[0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    reply = chat("yes")
    assert "expired" in reply["answer"]
    assert chat.database.leave_requests == []


def test_a_new_apply_replaces_the_old_waiting_one(chat):
    chat("apply", tool_message("apply_leave", **APPLY))
    chat("apply again", tool_message("apply_leave", **dict(APPLY, start_date="2026-11-11", end_date="2026-11-11")))
    waiting = [a for a in chat.database.actions if a["status"] == "waiting"]
    assert len(waiting) == 1 and waiting[0]["payload"]["start_date"] == "2026-11-11"


def test_a_document_attached_before_yes_is_saved_with_the_leave(chat):
    reply = chat("apply sick", tool_message("apply_leave", **dict(APPLY, leave_type="sick")))
    leave_service.attach_document_to_action(chat.database.employees[3], reply["pending_action"]["id"], "3/note.pdf")
    chat("yes")
    assert chat.database.leave_requests[0]["document_path"] == "3/note.pdf"


def test_other_messages_while_waiting_do_not_lose_the_request(chat):
    chat("apply", tool_message("apply_leave", **APPLY))
    chat("what is the notice period?", tool_message("search_policy"))
    chat("yes")
    assert len(chat.database.leave_requests) == 1


def test_confirmation_checks_the_rules_again(chat):
    chat("apply", tool_message("apply_leave", **APPLY))
    chat.database.add_leave(3, "sick", "2026-11-10", "2026-11-10", 1, status="approved")      # something else took the date meanwhile
    reply = chat("yes")
    assert "overlaps" in reply["answer"]
    assert [r["leave_type"] for r in chat.database.leave_requests] == ["sick"]


def test_a_broken_rule_is_a_normal_reply(chat):
    reply = chat("leave on Saturday", tool_message("apply_leave", **dict(APPLY, start_date="2026-11-14", end_date="2026-11-14")))
    assert "weekend or a company holiday" in reply["answer"]
    assert reply["pending_action"] is None


def test_maternity_cannot_be_applied_in_the_chat(chat):
    reply = chat("apply maternity", tool_message("apply_leave", **dict(APPLY, leave_type="maternity")))
    assert "cannot be applied in the chat" in reply["answer"]


def test_missing_details_get_an_example_and_no_guessing(chat):
    for arguments in ({"leave_type": "", "start_date": "2026-11-10"}, {"leave_type": "casual", "start_date": ""}):
        reply = chat("apply leave", tool_message("apply_leave", **arguments))
        assert "for example: Apply casual leave for 10 Nov 2026" in reply["answer"]
    assert chat.database.actions == []


def test_a_garbled_date_from_the_model_is_explained(chat):
    reply = chat("apply", tool_message("apply_leave", **dict(APPLY, start_date="next tuesday")))
    assert "could not read the start date" in reply["answer"]


def test_broken_tool_arguments_do_not_crash(chat):
    reply = chat("apply", {"tool_calls": [{"function": {"name": "apply_leave", "arguments": "{not json"}}]})
    assert "for example" in reply["answer"]


# ------------------------------------------------------------------ cancel
def test_cancel_by_date_asks_then_cancels(chat):
    leave = chat.database.add_leave(3, "casual", "2026-11-20", "2026-11-20", 1, status="pending")
    reply = chat("cancel my leave on 20 Nov", tool_message("cancel_leave", start_date="2026-11-20"))
    assert "Cancel Casual Leave on Fri, 20 Nov 2026" in reply["answer"]
    assert reply["pending_action"]["allows_document"] is False
    assert leave["status"] == "pending"                                                       # nothing happens before "yes"
    assert "was cancelled" in chat("yes")["answer"]
    assert leave["status"] == "cancelled"


def test_cancel_without_details_asks_which_one(chat):
    reply = chat("cancel my leave", tool_message("cancel_leave"))
    assert "Which leave do you want to cancel?" in reply["answer"]


def test_nobody_can_cancel_someone_elses_leave_through_the_model(chat):
    other = chat.database.add_leave(4, "casual", "2026-11-20", "2026-11-20", 1, status="pending")
    reply = chat("cancel leave", tool_message("cancel_leave", leave_id=other["id"]))
    assert "could not find that leave" in reply["answer"]
    assert chat.database.actions == []


def test_an_empty_message_is_refused(chat):
    with pytest.raises(ValueError):
        chat("   ")
