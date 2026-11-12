"""Tests for app/auth.py. A fake in-memory store replaces Supabase, so nothing real is called."""
import copy
from datetime import timedelta

import pytest

from app.identity import auth as auth


class FakeStore:
    """Behaves like employee_store, but keeps everything in two dictionaries."""

    def __init__(self):
        self.employees = {}
        self.logins = {}

    def add_employee(self, employee_id=1, emp_id="EMP001", dob="1995-03-07", is_active=True, full_name="Asha Rao"):
        self.employees[employee_id] = {
            "id": employee_id, "emp_id": emp_id, "full_name": full_name, "email": f"{emp_id}@x.com",
            "dept": "IT", "dob": dob, "is_active": is_active,
        }

    def find_employee_with_login(self, emp_id=None, employee_id=None):
        for employee in self.employees.values():
            if employee["emp_id"] == emp_id or employee["id"] == employee_id:
                found = dict(employee)
                found["login"] = copy.deepcopy(self.logins.get(employee["id"]))
                return found
        return None

    def insert_login(self, employee_id, password_hash, role):
        self.logins[employee_id] = {
            "password_hash": password_hash, "role": role, "must_change_password": True,
            "failed_login_attempts": 0, "locked_until": None, "password_changed_at": None,
        }

    def update_login(self, employee_id, fields):
        self.logins[employee_id].update(fields)


@pytest.fixture
def store(monkeypatch):
    fake_store = FakeStore()
    monkeypatch.setattr(auth.employee_store, "find_employee_with_login", fake_store.find_employee_with_login)
    monkeypatch.setattr(auth.employee_store, "insert_login", fake_store.insert_login)
    monkeypatch.setattr(auth.employee_store, "update_login", fake_store.update_login)
    monkeypatch.setenv("JWT_SECRET_KEY", "a-test-secret-key-that-is-long-enough-123456")
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)   # fast hashing for tests
    fake_store.add_employee()
    return fake_store


def set_password(store, password, employee_id=1, **extra_fields):
    """Give the fake employee a login with a known password."""
    store.insert_login(employee_id, auth.hash_password(password), "employee")
    store.logins[employee_id]["must_change_password"] = False
    store.logins[employee_id].update(extra_fields)


def get_error(function, *arguments):
    with pytest.raises(auth.AuthError) as caught:
        function(*arguments)
    return caught.value


# ------------------------------------------------------------------ passwords
def test_temporary_password_has_dob_emp_id_and_random_part():
    password = auth.build_temporary_password("1995-03-07", "EMP001")
    assert password.startswith("07031995EMP001")
    random_part = password[len("07031995EMP001"):]
    assert len(random_part) == auth.RANDOM_PART_LENGTH
    assert set(random_part) <= set(auth.RANDOM_PART_CHARACTERS)


def test_temporary_password_accepts_a_full_timestamp_and_is_random():
    first = auth.build_temporary_password("1995-03-07T00:00:00+00:00", "EMP001")
    second = auth.build_temporary_password("1995-03-07", "EMP001")
    assert first.startswith("07031995EMP001")
    assert first != second


def test_hash_and_verify_password():
    password_hash = auth.hash_password("correct horse 123")
    assert password_hash != "correct horse 123"
    assert auth.verify_password("correct horse 123", password_hash)
    assert not auth.verify_password("wrong password", password_hash)


def test_too_long_password_is_rejected():
    long_password = "a" * (auth.MAXIMUM_PASSWORD_BYTES + 1)
    with pytest.raises(ValueError):
        auth.hash_password(long_password)
    assert auth.verify_password(long_password, auth.hash_password("short")) is False


def test_parse_timestamp_handles_supabase_formats():
    assert auth.parse_timestamp(None) is None
    assert auth.parse_timestamp("2026-11-06T13:20:00Z").hour == 13
    assert auth.parse_timestamp("2026-11-06T13:20:00.12345+00:00").microsecond == 123450
    assert auth.parse_timestamp("2026-11-06T13:20:00.123+05:30").utcoffset() == timedelta(hours=5, minutes=30)


# ------------------------------------------------------------------ tokens
def test_token_round_trip_returns_the_current_employee(store):
    set_password(store, "password-123456")
    token = auth.create_access_token(1)
    employee = auth.load_employee_from_token(token)
    assert employee["emp_id"] == "EMP001"
    assert employee["role"] == "employee"
    assert employee["must_change_password"] is False


def test_garbage_token_is_rejected(store):
    assert get_error(auth.load_employee_from_token, "not-a-token").status_code == 401


def test_token_signed_with_another_secret_is_rejected(store, monkeypatch):
    set_password(store, "password-123456")
    token = auth.create_access_token(1)
    monkeypatch.setenv("JWT_SECRET_KEY", "a-completely-different-secret-key-9876543210")
    assert get_error(auth.load_employee_from_token, token).status_code == 401


def test_expired_token_is_rejected(store, monkeypatch):
    set_password(store, "password-123456")
    monkeypatch.setattr(auth, "TOKEN_VALID_MINUTES", -1)
    token = auth.create_access_token(1)
    assert get_error(auth.load_employee_from_token, token).status_code == 401


def test_token_of_a_deactivated_employee_is_rejected(store):
    set_password(store, "password-123456")
    token = auth.create_access_token(1)
    store.employees[1]["is_active"] = False
    assert get_error(auth.load_employee_from_token, token).status_code == 401


def test_secret_key_must_be_long_enough(store, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "short")
    with pytest.raises(RuntimeError):
        auth.create_access_token(1)


# ------------------------------------------------------------------ login
def test_login_success_returns_token_and_details(store):
    set_password(store, "password-123456", must_change_password=True)
    result = auth.authenticate("EMP001", "password-123456")
    assert result["token_type"] == "bearer"
    assert result["must_change_password"] is True
    assert result["full_name"] == "Asha Rao"
    assert auth.load_employee_from_token(result["access_token"])["emp_id"] == "EMP001"


def test_wrong_password_and_unknown_id_give_the_same_message(store):
    set_password(store, "password-123456")
    wrong_password = get_error(auth.authenticate, "EMP001", "wrong")
    unknown_id = get_error(auth.authenticate, "NOBODY", "password-123456")
    assert wrong_password.status_code == unknown_id.status_code == 401
    assert wrong_password.message == unknown_id.message == auth.LOGIN_FAILED_MESSAGE


def test_inactive_employee_cannot_log_in(store):
    set_password(store, "password-123456")
    store.employees[1]["is_active"] = False
    assert get_error(auth.authenticate, "EMP001", "password-123456").message == auth.LOGIN_FAILED_MESSAGE


def test_employee_without_a_login_cannot_log_in(store):
    assert get_error(auth.authenticate, "EMP001", "anything").status_code == 401


def test_wrong_password_is_counted(store):
    set_password(store, "password-123456")
    get_error(auth.authenticate, "EMP001", "wrong")
    get_error(auth.authenticate, "EMP001", "wrong")
    assert store.logins[1]["failed_login_attempts"] == 2


def test_account_locks_after_too_many_failures(store):
    set_password(store, "password-123456")
    for _ in range(auth.MAXIMUM_FAILED_ATTEMPTS):
        get_error(auth.authenticate, "EMP001", "wrong")
    # Even the correct password is refused while the account is locked.
    assert get_error(auth.authenticate, "EMP001", "password-123456").status_code == 429


def test_old_lock_does_not_block_login(store):
    past = (auth.get_current_time() - timedelta(minutes=1)).isoformat()
    set_password(store, "password-123456", locked_until=past, failed_login_attempts=0)
    result = auth.authenticate("EMP001", "password-123456")
    assert result["access_token"]
    assert store.logins[1]["locked_until"] is None


def test_successful_login_clears_failed_attempts(store):
    set_password(store, "password-123456", failed_login_attempts=3)
    auth.authenticate("EMP001", "password-123456")
    assert store.logins[1]["failed_login_attempts"] == 0


# ------------------------------------------------------------------ create and reset credentials
def test_create_credentials_stores_a_hash_not_the_password(store):
    result = auth.create_credentials("EMP001", "hr")
    stored = store.logins[1]
    assert result["temporary_password"].startswith("07031995EMP001")
    assert stored["password_hash"] != result["temporary_password"]
    assert auth.verify_password(result["temporary_password"], stored["password_hash"])
    assert stored["role"] == "hr"
    assert stored["must_change_password"] is True


def test_create_credentials_errors(store):
    assert get_error(auth.create_credentials, "NOBODY", "employee").status_code == 404
    assert get_error(auth.create_credentials, "EMP001", "boss").status_code == 400
    store.employees[1]["dob"] = None
    assert get_error(auth.create_credentials, "EMP001", "employee").status_code == 400
    store.employees[1]["dob"] = "1995-03-07"
    store.employees[1]["is_active"] = False
    assert get_error(auth.create_credentials, "EMP001", "employee").status_code == 400
    store.employees[1]["is_active"] = True
    auth.create_credentials("EMP001", "employee")
    assert get_error(auth.create_credentials, "EMP001", "employee").status_code == 409


def test_reset_password_gives_a_new_temporary_password_and_unlocks(store):
    set_password(store, "old-password-123", failed_login_attempts=4, locked_until="2999-01-01T00:00:00+00:00")
    result = auth.reset_password("EMP001")
    assert auth.authenticate("EMP001", result["temporary_password"])["must_change_password"] is True
    assert get_error(auth.authenticate, "EMP001", "old-password-123").status_code == 401
    assert store.logins[1]["locked_until"] is None


def test_reset_password_needs_an_existing_login(store):
    assert get_error(auth.reset_password, "EMP001").status_code == 404
    assert get_error(auth.reset_password, "NOBODY").status_code == 404


# ------------------------------------------------------------------ change password
def test_change_password_success(store):
    set_password(store, "old-password-123", must_change_password=True)
    auth.change_password(1, "old-password-123", "brand-new-password")
    assert store.logins[1]["must_change_password"] is False
    assert store.logins[1]["password_changed_at"]
    assert auth.authenticate("EMP001", "brand-new-password")["must_change_password"] is False
    assert get_error(auth.authenticate, "EMP001", "old-password-123").status_code == 401


def test_change_password_rejects_a_wrong_current_password_and_counts_it(store):
    set_password(store, "old-password-123")
    assert get_error(auth.change_password, 1, "wrong", "brand-new-password").status_code == 401
    assert store.logins[1]["failed_login_attempts"] == 1


def test_change_password_rules(store):
    set_password(store, "old-password-123")
    assert get_error(auth.change_password, 1, "old-password-123", "short").status_code == 400
    assert get_error(auth.change_password, 1, "old-password-123", "x" * 80).status_code == 400
    assert get_error(auth.change_password, 1, "old-password-123", "old-password-123").status_code == 400


def test_old_token_stops_working_after_a_password_change(store, monkeypatch):
    set_password(store, "old-password-123")
    real_clock = auth.get_current_time
    monkeypatch.setattr(auth, "get_current_time", lambda: real_clock() - timedelta(hours=1))
    old_token = auth.create_access_token(1)       # made an hour ago
    monkeypatch.setattr(auth, "get_current_time", real_clock)

    assert auth.load_employee_from_token(old_token)["emp_id"] == "EMP001"
    auth.change_password(1, "old-password-123", "brand-new-password")
    assert get_error(auth.load_employee_from_token, old_token).status_code == 401
