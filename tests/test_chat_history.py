"""Tests for the saved chat history: saving, the newest-10 limit, own messages only, and what the AI is shown."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.rag.answer import assistant as assistant
from app.chat import store as chat_store
from app import main

client = TestClient(main.app)
HEADERS = {"Authorization": "Bearer any-token"}
EMPLOYEE = {"employee_id": 4, "emp_id": "EMP004", "full_name": "Karan Mehta", "dept": "IT", "role": "employee", "must_change_password": False}


@pytest.fixture(autouse=True)
def logged_in(monkeypatch):
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: EMPLOYEE)


class FakeChatTable:
    """Stands in for the chat_messages table (and for requests) so no Supabase call is made.

    Trims to the newest MESSAGES_KEPT_PER_EMPLOYEE on insert, the way the chat_messages_trim
    trigger in sql/09_chat_history.sql does, so these tests see what production really stores.
    """

    def __init__(self, monkeypatch):
        self.rows = []
        self.last_params = None
        self.next_id = 1
        monkeypatch.setattr(chat_store, "read_rows", self.read_rows)
        monkeypatch.setattr(chat_store.requests, "post", self.post)
        monkeypatch.setattr(chat_store, "check_response", lambda response, text: None)

    def post(self, url, headers=None, json=None, timeout=None):
        saved = []
        for row in json:
            saved.append(dict(row, id=self.next_id))
            self.next_id += 1
            self.rows.append(saved[-1])
            self.trim(row["employee_id"])
        return type("Response", (), {"json": lambda self_: saved})()

    def trim(self, employee_id):
        """What the trigger does: delete everything but the newest few of this employee."""
        theirs = sorted([r for r in self.rows if r["employee_id"] == employee_id], key=lambda r: r["id"])
        for row in theirs[: -chat_store.MESSAGES_KEPT_PER_EMPLOYEE]:
            self.rows.remove(row)

    def read_rows(self, table, params, description):
        self.last_params = params
        rows = [r for r in self.rows if f"eq.{r['employee_id']}" == params["employee_id"]]
        if "id" in params:
            rows = [r for r in rows if r["id"] < int(params["id"].replace("lt.", ""))]
        return sorted(rows, key=lambda r: r["id"], reverse=True)[: params["limit"]]


@pytest.fixture
def table(monkeypatch):
    return FakeChatTable(monkeypatch)


def save(table_object, employee_id, text):
    now = datetime.now(timezone.utc)
    chat_store.save_exchange(employee_id, text, now, "answer to " + text, [{"number": 1}], now + timedelta(seconds=2))


def test_an_exchange_is_saved_as_two_rows_with_their_own_times(table):
    asked = datetime(2026, 11, 9, 17, 0, 0, tzinfo=timezone.utc)
    question_row, answer_row = chat_store.save_exchange(4, "hello", asked, "hi", [{"number": 1}], asked + timedelta(seconds=3))
    assert (question_row["sender"], question_row["message"], question_row["created_at"]) == ("user", "hello", "2026-11-09T17:00:00+00:00")
    assert (answer_row["sender"], answer_row["sources"], answer_row["created_at"]) == ("assistant", [{"number": 1}], "2026-11-09T17:00:03+00:00")


def test_history_is_oldest_first_and_paged(table):
    for number in range(1, 6):
        save(table, 4, f"question {number}")
    newest = chat_store.list_messages(4, 4)
    assert [row["id"] for row in newest] == [7, 8, 9, 10]
    assert [row["id"] for row in chat_store.list_messages(4, 4, before_id=7)] == [3, 4, 5, 6]
    assert "created_at" not in table.last_params      # no age filter: the newest few are always shown


def test_only_the_newest_messages_are_kept(table):
    kept = chat_store.MESSAGES_KEPT_PER_EMPLOYEE
    for number in range(1, 8):                        # 7 exchanges = 14 rows, 4 more than the limit
        save(table, 4, f"question {number}")
    assert len(table.rows) == kept
    everything = chat_store.list_messages(4, 100)
    assert [row["id"] for row in everything] == list(range(15 - kept, 15))
    assert everything[0]["message"] == "question 3"    # questions 1 and 2 and their answers are gone
    assert everything[-1]["message"] == "answer to question 7"


def test_the_limit_is_per_employee(table):
    for number in range(1, 8):
        save(table, 4, f"mine {number}")
    save(table, 5, "theirs")
    assert len(chat_store.list_messages(4, 100)) == chat_store.MESSAGES_KEPT_PER_EMPLOYEE
    assert [row["message"] for row in chat_store.list_messages(5, 100)] == ["theirs", "answer to theirs"]


def test_the_history_endpoint_returns_only_my_messages(table):
    save(table, 4, "mine")
    save(table, 5, "someone else")
    response = client.get("/chat/history", headers=HEADERS)
    assert response.status_code == 200
    assert [row["message"] for row in response.json()] == ["mine", "answer to mine"]
    assert client.get("/chat/history").status_code == 401


def test_chat_saves_the_exchange_and_returns_both_times(table, monkeypatch):
    monkeypatch.setattr(main, "answer_chat_message", lambda employee, question, history=None: {
        "question": question, "rewritten_question": question, "answer": "Fine.", "answerable": True, "sources": [], "pending_action": None})
    response = client.post("/chat", headers=HEADERS, json={"question": "  Hello  "})
    body = response.json()
    assert response.status_code == 200 and body["answer"] == "Fine."
    assert [row["message"] for row in table.rows] == ["Hello", "Fine."]
    assert body["asked_at"] and body["answered_at"]


def test_the_answer_is_still_sent_when_saving_fails(table, monkeypatch):
    monkeypatch.setattr(main, "answer_chat_message", lambda employee, question, history=None: {
        "question": question, "rewritten_question": question, "answer": "Fine.", "answerable": True, "sources": [], "pending_action": None})
    monkeypatch.setattr(main.chat_store, "save_exchange", lambda *args: (_ for _ in ()).throw(RuntimeError("database down")))
    response = client.post("/chat", headers=HEADERS, json={"question": "Hello"})
    assert response.status_code == 200 and response.json()["answer"] == "Fine."
    assert response.json()["asked_at"] is None


def test_the_ai_gets_the_earlier_messages_before_the_new_question(table, monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "answer_chat_message", lambda employee, question, history=None: seen.update(history=history) or {
        "question": question, "rewritten_question": question, "answer": "ok", "answerable": True, "sources": []})
    save(table, 4, "how many casual leaves")
    client.post("/chat", headers=HEADERS, json={"question": "and sick leave?"})
    assert [row["message"] for row in seen["history"]] == ["how many casual leaves", "answer to how many casual leaves"]


def test_history_messages_for_the_model_are_short_and_limited():
    rows = [{"sender": "user" if number % 2 else "assistant", "message": "x" * 2000} for number in range(10)]
    messages = assistant.build_history_messages(rows)
    assert len(messages) == assistant.MAXIMUM_HISTORY_MESSAGES
    assert all(len(message["content"]) == assistant.MAXIMUM_HISTORY_MESSAGE_LENGTH for message in messages)
    assert {message["role"] for message in messages} == {"user", "assistant"}
    assert assistant.build_history_messages(None) == []


def test_a_follow_up_is_searched_as_a_full_question(monkeypatch):
    monkeypatch.setattr(assistant.leave_store, "get_employee_row", lambda employee_id: {"id": employee_id, "is_active": True})
    monkeypatch.setattr(assistant.leave_store, "get_waiting_action", lambda employee_id: None)
    monkeypatch.setattr(assistant, "call_router", lambda question, history=None: {"tool_calls": [{"function": {
        "name": "search_policy", "arguments": json.dumps({"standalone_question": "How many sick leaves per year?"})}}]})
    asked = {}
    monkeypatch.setattr(assistant, "answer_question", lambda question: asked.update(text=question) or {
        "question": question, "rewritten_question": question, "answer": "12", "answerable": True, "sources": []})
    result = assistant.answer_chat_message(EMPLOYEE, "and sick?", [{"sender": "user", "message": "How many casual leaves per year?"}])
    assert asked["text"] == "How many sick leaves per year?"
    assert result["question"] == "and sick?"
