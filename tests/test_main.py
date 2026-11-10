"""Tests for app/main.py. answer_question and the login functions are replaced by fakes,
so no real Groq or Supabase calls are made."""
import pytest
from fastapi.testclient import TestClient

from app.identity import auth as auth
from app import main
from app.api.limits import MAXIMUM_QUESTION_LENGTH

client = TestClient(main.app)

TOKEN_HEADERS = {"Authorization": "Bearer any-token"}


def make_employee(role="employee", must_change_password=False):
    return {
        "employee_id": 1, "emp_id": "EMP001", "full_name": "Asha Rao", "dept": "IT",
        "role": role, "must_change_password": must_change_password,
    }


def make_fake_result():
    return {
        "question": "How many casual leaves do I get?",
        "rewritten_question": "How many casual leaves are allowed per year?",
        "answer": "You get 10 paid working days per calendar year [1].",
        "answerable": True,
        "sources": [{"number": 1, "chunk_id": "HR-LEAVE-001#leave-types#00", "doc_id": "HR-LEAVE-001"}],
    }


@pytest.fixture
def logged_in(monkeypatch):
    """Makes every request look like it comes from a valid token of this employee. Change .employee to test roles."""
    class LoggedIn:
        employee = make_employee()

    holder = LoggedIn()
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: holder.employee)
    return holder


@pytest.fixture(autouse=True)
def no_chat_history(monkeypatch):
    """The chat history lives in Supabase, so every test here uses an empty, do-nothing history by default."""
    monkeypatch.setattr(main.chat_store, "list_messages", lambda employee_id, limit, before_id=None: [])
    monkeypatch.setattr(main.chat_store, "delete_expired_messages_once_an_hour", lambda: None)

    def fake_save_exchange(employee_id, question_text, asked_at, answer_text, sources, answered_at):
        return {"id": 1, "created_at": asked_at.isoformat()}, {"id": 2, "created_at": answered_at.isoformat()}

    monkeypatch.setattr(main.chat_store, "save_exchange", fake_save_exchange)


# ------------------------------------------------------------------ health and login
def test_health_check_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_returns_the_token(monkeypatch):
    result = {"access_token": "abc", "token_type": "bearer", "must_change_password": True,
              "role": "employee", "full_name": "Asha Rao", "emp_id": "EMP001"}
    monkeypatch.setattr(main.auth, "authenticate", lambda emp_id, password: result)
    response = client.post("/login", json={"emp_id": "EMP001", "password": "secret"})
    assert response.status_code == 200
    assert response.json()["access_token"] == "abc"
    assert response.json()["must_change_password"] is True


def test_login_failure_is_a_401_with_the_message(monkeypatch):
    def fake_authenticate(emp_id, password):
        raise auth.AuthError(401, auth.LOGIN_FAILED_MESSAGE)

    monkeypatch.setattr(main.auth, "authenticate", fake_authenticate)
    response = client.post("/login", json={"emp_id": "EMP001", "password": "wrong"})
    assert response.status_code == 401
    assert response.json()["detail"] == auth.LOGIN_FAILED_MESSAGE
    assert response.headers["www-authenticate"] == "Bearer"


def test_login_requires_both_fields():
    assert client.post("/login", json={"emp_id": "EMP001"}).status_code == 422
    assert client.post("/login", json={"password": "x"}).status_code == 422


# ------------------------------------------------------------------ token checks
def test_chat_without_a_token_is_401():
    response = client.post("/chat", json={"question": "Hello"})
    assert response.status_code == 401


def test_chat_with_an_invalid_token_is_401(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "a-test-secret-key-that-is-long-enough-123456")
    response = client.post("/chat", json={"question": "Hello"}, headers=TOKEN_HEADERS)
    assert response.status_code == 401


def test_chat_is_blocked_until_the_temporary_password_is_changed(logged_in):
    logged_in.employee = make_employee(must_change_password=True)
    response = client.post("/chat", json={"question": "Hello"}, headers=TOKEN_HEADERS)
    assert response.status_code == 403
    assert "change your temporary password" in response.json()["detail"]


def test_change_password_works_even_with_a_temporary_password(logged_in, monkeypatch):
    logged_in.employee = make_employee(must_change_password=True)
    received = {}

    def fake_change_password(employee_id, current_password, new_password):
        received.update(employee_id=employee_id, current=current_password, new=new_password)

    monkeypatch.setattr(main.auth, "change_password", fake_change_password)
    response = client.post(
        "/change-password",
        json={"current_password": "old-temp", "new_password": "brand-new-password"},
        headers=TOKEN_HEADERS,
    )
    assert response.status_code == 200
    assert received == {"employee_id": 1, "current": "old-temp", "new": "brand-new-password"}


def test_change_password_without_a_token_is_401():
    response = client.post("/change-password", json={"current_password": "a", "new_password": "b"})
    assert response.status_code == 401


# ------------------------------------------------------------------ HR only endpoints
def test_register_is_blocked_for_a_normal_employee(logged_in, monkeypatch):
    monkeypatch.setattr(main.auth, "create_credentials", lambda emp_id, role: pytest.fail("must not be called"))
    response = client.post("/register", json={"emp_id": "EMP002"}, headers=TOKEN_HEADERS)
    assert response.status_code == 403


def test_register_is_blocked_for_the_ceo_role(logged_in):
    logged_in.employee = make_employee(role="ceo")
    assert client.post("/register", json={"emp_id": "EMP002"}, headers=TOKEN_HEADERS).status_code == 403


def test_register_works_for_hr(logged_in, monkeypatch):
    logged_in.employee = make_employee(role="hr")
    received = {}

    def fake_create_credentials(emp_id, role):
        received.update(emp_id=emp_id, role=role)
        return {"emp_id": emp_id, "full_name": "New Person", "role": role, "temporary_password": "07031995EMP002abcd2345"}

    monkeypatch.setattr(main.auth, "create_credentials", fake_create_credentials)
    response = client.post("/register", json={"emp_id": "EMP002", "role": "ceo"}, headers=TOKEN_HEADERS)
    assert response.status_code == 200
    assert response.json()["temporary_password"] == "07031995EMP002abcd2345"
    assert received == {"emp_id": "EMP002", "role": "ceo"}


def test_register_rejects_an_unknown_role(logged_in):
    logged_in.employee = make_employee(role="hr")
    response = client.post("/register", json={"emp_id": "EMP002", "role": "admin"}, headers=TOKEN_HEADERS)
    assert response.status_code == 422


def test_register_shows_the_error_from_auth(logged_in, monkeypatch):
    logged_in.employee = make_employee(role="hr")

    def fake_create_credentials(emp_id, role):
        raise auth.AuthError(409, "This employee already has a login. Use reset-password instead.")

    monkeypatch.setattr(main.auth, "create_credentials", fake_create_credentials)
    response = client.post("/register", json={"emp_id": "EMP002"}, headers=TOKEN_HEADERS)
    assert response.status_code == 409


def test_reset_password_is_hr_only(logged_in, monkeypatch):
    assert client.post("/reset-password", json={"emp_id": "EMP002"}, headers=TOKEN_HEADERS).status_code == 403

    logged_in.employee = make_employee(role="hr")
    monkeypatch.setattr(main.auth, "reset_password", lambda emp_id: {
        "emp_id": emp_id, "full_name": "X", "role": "employee", "temporary_password": "newtemp"})
    response = client.post("/reset-password", json={"emp_id": "EMP002"}, headers=TOKEN_HEADERS)
    assert response.status_code == 200
    assert response.json()["temporary_password"] == "newtemp"


# ------------------------------------------------------------------ chat
def test_chat_returns_the_answer_and_sources(logged_in, monkeypatch):
    monkeypatch.setattr(main, "answer_chat_message", lambda employee, question, history=None: make_fake_result())
    response = client.post("/chat", json={"question": "How many casual leaves do I get?"}, headers=TOKEN_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["answerable"] is True
    assert body["answer"].startswith("You get 10")
    assert body["sources"][0]["doc_id"] == "HR-LEAVE-001"


def test_chat_passes_the_employee_and_question_to_the_assistant(logged_in, monkeypatch):
    received_questions = []

    def fake_answer_chat_message(employee, question, history=None):
        received_questions.append((employee["emp_id"], question))
        return make_fake_result()

    monkeypatch.setattr(main, "answer_chat_message", fake_answer_chat_message)
    client.post("/chat", json={"question": "What is the notice period?"}, headers=TOKEN_HEADERS)
    assert received_questions == [("EMP001", "What is the notice period?")]


def test_chat_rejects_bad_questions(logged_in):
    assert client.post("/chat", json={}, headers=TOKEN_HEADERS).status_code == 422
    assert client.post("/chat", json={"question": ""}, headers=TOKEN_HEADERS).status_code == 422
    long_question = "a" * (MAXIMUM_QUESTION_LENGTH + 1)
    assert client.post("/chat", json={"question": long_question}, headers=TOKEN_HEADERS).status_code == 422


def test_chat_returns_400_for_a_blank_question(logged_in, monkeypatch):
    def fake_answer_chat_message(employee, question, history=None):
        raise ValueError("The question is empty.")

    monkeypatch.setattr(main, "answer_chat_message", fake_answer_chat_message)
    response = client.post("/chat", json={"question": "   "}, headers=TOKEN_HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"] == "The question is empty."


def test_chat_returns_502_and_hides_the_real_error(logged_in, monkeypatch):
    def fake_answer_chat_message(employee, question, history=None):
        raise RuntimeError("secret internal detail: key sb_secret_123")

    monkeypatch.setattr(main, "answer_chat_message", fake_answer_chat_message)
    response = client.post("/chat", json={"question": "Any question"}, headers=TOKEN_HEADERS)
    assert response.status_code == 502
    assert "secret" not in response.text
