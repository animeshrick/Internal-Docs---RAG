"""Tests for who may ask about whom (people_service) and the chat tools that use it."""
import json

import pytest

from app.rag.answer import assistant as assistant
import people_service
import people_store

NO_ANSWER = "I don't have an answer for this question."
PEOPLE = {
    "EMP-1001": {"id": 1, "emp_id": "EMP-1001", "full_name": "Priya Sen", "role": "hr", "is_confidential": False},
    "EMP-1004": {"id": 4, "emp_id": "EMP-1004", "full_name": "Ananya Das", "role": "employee", "is_confidential": False},
    "EMP-1008": {"id": 8, "emp_id": "EMP-1008", "full_name": "Parvati Chouhan", "role": "ceo", "is_confidential": False},
    "EMP-1010": {"id": 10, "emp_id": "EMP-1010", "full_name": "Rohan Mehta", "role": "employee", "is_confidential": True},
}
HR = {"employee_id": 1, "emp_id": "EMP-1001", "full_name": "Priya Sen", "role": "hr"}
EMPLOYEE = {"employee_id": 4, "emp_id": "EMP-1004", "full_name": "Ananya Das", "role": "employee"}


@pytest.fixture(autouse=True)
def fake_people(monkeypatch):
    monkeypatch.setattr(people_store, "find_employee", lambda text: next(
        (dict(p) for p in PEOPLE.values() if (text or "").strip() in (p["emp_id"], p["emp_id"].replace("-", ""), p["full_name"])), None))


def test_everyone_can_ask_about_themselves():
    for person in (None, "", "me", "my", "EMP-1004", "EMP1004", "Ananya Das"):
        assert people_service.resolve_target(EMPLOYEE, person) is None


def test_hr_gets_the_other_employee_by_id_or_name():
    for person in ("EMP-1004", "EMP1004", "Ananya Das"):
        assert people_service.resolve_target(HR, person)["emp_id"] == "EMP-1004"


@pytest.mark.parametrize("person", ["CEO", "the CTO", "Parvati Chouhan", "EMP-1008", "Rohan Mehta", "EMP-1010"])
def test_hr_never_gets_the_ceo_or_cto(person):
    with pytest.raises(people_service.PeopleError) as error:
        people_service.resolve_target(HR, person)
    assert error.value.message == NO_ANSWER


def test_hr_gets_a_not_found_message_for_an_unknown_person():
    with pytest.raises(people_service.PeopleError) as error:
        people_service.resolve_target(HR, "Nobody Here")
    assert error.value.message == people_service.NOT_FOUND


def test_a_normal_employee_cannot_ask_about_others():
    for person in ("EMP-1001", "Priya Sen", "EMP-9999"):
        with pytest.raises(people_service.PeopleError) as error:
            people_service.resolve_target(EMPLOYEE, person)
        assert error.value.message == NO_ANSWER


# ------------------------------------------------------------------ through the chat
@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setattr(assistant.leave_store, "get_employee_row", lambda employee_id: {"id": employee_id, "is_active": True})
    monkeypatch.setattr(assistant.leave_store, "get_waiting_action", lambda employee_id: None)
    monkeypatch.setattr(assistant, "build_balance_text", lambda row, label=None: f"balance of row {row['id']} label {label}")
    monkeypatch.setattr(assistant, "build_leave_list_text", lambda row, status, label=None: f"list of row {row['id']} label {label}")
    monkeypatch.setattr(assistant, "answer_with_employee_data", lambda question, data: assistant.build_reply(question, data))
    monkeypatch.setattr(people_service, "build_profile_text", lambda target: f"profile of {target['emp_id']}")
    monkeypatch.setattr(people_store, "get_profile", lambda employee_id: {"emp_id": "OWN", "id": employee_id})
    state = {}
    monkeypatch.setattr(assistant, "call_router", lambda question, history=None: state["reply"])

    def send(employee, tool_name, **arguments):
        state["reply"] = {"tool_calls": [{"function": {"name": tool_name, "arguments": json.dumps(arguments)}}]}
        return assistant.answer_chat_message(employee, "question")["answer"]
    return send


def test_hr_reads_another_employees_leave_with_an_owner_label(chat):
    assert chat(HR, "get_leave_balance", person="EMP-1004") == "balance of row 4 label Ananya Das (EMP-1004)"
    assert chat(HR, "list_my_leaves", person="EMP1004", status="any") == "list of row 4 label Ananya Das (EMP-1004)"
    assert chat(HR, "get_employee_info", person="Ananya Das") == "profile of EMP-1004"


def test_own_data_has_no_owner_label(chat):
    assert chat(EMPLOYEE, "get_leave_balance") == "balance of row 4 label None"
    assert chat(EMPLOYEE, "get_employee_info") == "profile of OWN"


def test_chat_refuses_ceo_cto_and_other_people(chat):
    for tool in ("get_leave_balance", "list_my_leaves", "get_employee_info"):
        assert chat(HR, tool, person="CEO") == NO_ANSWER
        assert chat(HR, tool, person="Rohan Mehta") == NO_ANSWER
        assert chat(EMPLOYEE, tool, person="EMP-1001") == NO_ANSWER
