"""
auth.py - passwords, login tokens, lockout and credential management.

How login works:
    1. HR creates a login for an employee with create_credentials(). The employee gets a TEMPORARY password
       made of: date of birth (DDMMYYYY) + emp_id + 8 random characters.
    2. The employee calls authenticate() (the /login endpoint) and receives a token.
    3. With a temporary password the person must call change_password() before using /chat.
    4. Every later request sends the token. load_employee_from_token() checks it and reloads the employee
       from the database, so a deactivated employee or a changed role takes effect immediately.

The database is only touched through app/hr/employees/store.py.
"""
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import bcrypt
import jwt

from app.hr.employees import store as employee_store
from app.core.config import get_required_setting

ALLOWED_ROLES = ("employee", "hr", "ceo", "pm")      # pm = project manager: approves leave, but has no HR powers

BCRYPT_ROUNDS = 12                   # higher = slower for attackers (and for us)
MAXIMUM_PASSWORD_BYTES = 72          # bcrypt cannot use more than 72 bytes
MINIMUM_NEW_PASSWORD_LENGTH = 10

MAXIMUM_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15

TOKEN_ALGORITHM = "HS256"
TOKEN_VALID_MINUTES = 480            # 8 hours
MINIMUM_SECRET_KEY_LENGTH = 32

RANDOM_PART_LENGTH = 8
# No 0, O, 1, l, I so a printed password is easy to read.
RANDOM_PART_CHARACTERS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"

# One message for "no such id", "wrong password" and "inactive", so nobody can learn which ids exist.
LOGIN_FAILED_MESSAGE = "Invalid employee ID or password."
SESSION_EXPIRED_MESSAGE = "Your session has expired or is not valid. Please log in again."


DEFAULT_ERROR_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 429: "too_many_attempts"}


class AuthError(Exception):
    """A problem the API should show to the client, with the HTTP status code to use."""

    def __init__(self, status_code, message, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        # A short machine-readable reason, so the website does not have to guess from the message text.
        self.code = code or DEFAULT_ERROR_CODES.get(status_code, "error")


def get_current_time():
    """Separate function so tests can control the clock."""
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ passwords
def hash_password(plain_password):
    """Turn a password into a bcrypt hash that is safe to store."""
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > MAXIMUM_PASSWORD_BYTES:
        raise ValueError(f"A password can be at most {MAXIMUM_PASSWORD_BYTES} bytes long.")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain_password, password_hash):
    """True when the password matches the stored hash."""
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > MAXIMUM_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(password_bytes, password_hash.encode("utf-8"))
    except ValueError:
        return False


@lru_cache(maxsize=1)
def get_dummy_password_hash():
    """
    A hash of a password nobody knows. For an unknown id we still check a password against it,
    so the answer takes the same time as for a real id.
    """
    return hash_password("this-is-not-a-real-password")


def build_temporary_password(date_of_birth_text, emp_id):
    """date of birth (DDMMYYYY) + emp_id + 8 random characters. Example: 07031995EMP001k7Qm2xPa"""
    date_of_birth = date.fromisoformat(str(date_of_birth_text)[:10])   # accepts "1995-03-07" or a full timestamp
    random_part = "".join(secrets.choice(RANDOM_PART_CHARACTERS) for _ in range(RANDOM_PART_LENGTH))
    return f"{date_of_birth:%d%m%Y}{emp_id}{random_part}"


def parse_timestamp(timestamp_text):
    """Read a timestamp that Supabase sent. Returns None for an empty value."""
    if not timestamp_text:
        return None
    text = timestamp_text.replace("Z", "+00:00")
    # Older Python versions only read exactly 3 or 6 digits after the seconds, so we pad to 6.
    match = re.match(r"^(.*\.)(\d+)(.*)$", text)
    if match:
        text = match.group(1) + match.group(2)[:6].ljust(6, "0") + match.group(3)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ------------------------------------------------------------------ tokens
def get_token_secret():
    secret_key = get_required_setting("JWT_SECRET_KEY")
    if len(secret_key) < MINIMUM_SECRET_KEY_LENGTH:
        raise RuntimeError(f"JWT_SECRET_KEY must be at least {MINIMUM_SECRET_KEY_LENGTH} characters long.")
    return secret_key


def create_access_token(employee_id):
    """A signed token that only says who the employee is. Role and status are read fresh from the database."""
    issued_at = get_current_time()
    payload = {
        "sub": str(employee_id),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=TOKEN_VALID_MINUTES),
    }
    return jwt.encode(payload, get_token_secret(), algorithm=TOKEN_ALGORITHM)


def decode_access_token(token):
    try:
        return jwt.decode(
            token, get_token_secret(), algorithms=[TOKEN_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError:
        raise AuthError(401, SESSION_EXPIRED_MESSAGE, "session_expired")


def load_employee_from_token(token):
    """Check the token and return the current employee (role, department, must_change_password ...)."""
    payload = decode_access_token(token)
    try:
        employee_id = int(payload["sub"])
    except ValueError:
        raise AuthError(401, SESSION_EXPIRED_MESSAGE, "session_expired")

    record = employee_store.find_employee_with_login(employee_id=employee_id)
    if record is None or record["login"] is None or not record["is_active"]:
        raise AuthError(401, SESSION_EXPIRED_MESSAGE, "session_expired")

    # A token made before the last password change or reset is no longer accepted.
    password_changed_at = parse_timestamp(record["login"].get("password_changed_at"))
    if password_changed_at is not None and payload["iat"] < int(password_changed_at.timestamp()):
        raise AuthError(401, SESSION_EXPIRED_MESSAGE, "session_expired")

    return {
        "employee_id": record["id"],
        "emp_id": record["emp_id"],
        "full_name": record["full_name"],
        "dept": record["dept"],
        "is_confidential": bool(record.get("is_confidential")),      # the CTO: no attendance, and hidden from HR
        "role": record["login"]["role"],
        "must_change_password": record["login"]["must_change_password"],
    }


# ------------------------------------------------------------------ login
def record_failed_attempt(employee_id, login):
    """Count a wrong password. After too many, lock the account for a while."""
    attempts = login["failed_login_attempts"] + 1
    if attempts >= MAXIMUM_FAILED_ATTEMPTS:
        locked_until = get_current_time() + timedelta(minutes=LOCK_MINUTES)
        fields = {"failed_login_attempts": 0, "locked_until": locked_until.isoformat()}
    else:
        fields = {"failed_login_attempts": attempts}
    employee_store.update_login(employee_id, fields)


def is_locked(login):
    locked_until = parse_timestamp(login.get("locked_until"))
    return locked_until is not None and locked_until > get_current_time()


def authenticate(emp_id, password):
    """Check emp_id and password. Returns the token and a few details, or raises AuthError."""
    emp_id = (emp_id or "").strip()
    record = employee_store.find_employee_with_login(emp_id=emp_id) if emp_id else None

    if record is None or record["login"] is None:
        verify_password(password, get_dummy_password_hash())   # same waiting time as a real check
        raise AuthError(401, LOGIN_FAILED_MESSAGE, "invalid_credentials")

    login = record["login"]
    if is_locked(login):
        raise AuthError(429, "Too many failed attempts. Please try again in a few minutes.")

    if not record["is_active"]:
        verify_password(password, get_dummy_password_hash())
        raise AuthError(401, LOGIN_FAILED_MESSAGE, "invalid_credentials")

    if not verify_password(password, login["password_hash"]):
        record_failed_attempt(record["id"], login)
        raise AuthError(401, LOGIN_FAILED_MESSAGE, "invalid_credentials")

    if login["failed_login_attempts"] or login["locked_until"]:
        employee_store.update_login(record["id"], {"failed_login_attempts": 0, "locked_until": None})

    return {
        "access_token": create_access_token(record["id"]),
        "token_type": "bearer",
        "must_change_password": login["must_change_password"],
        "role": login["role"],
        "full_name": record["full_name"],
        "emp_id": record["emp_id"],
        "dept": record["dept"],                                      # the website decides the menu from these two
        "is_confidential": bool(record.get("is_confidential")),
    }


# ------------------------------------------------------------------ credential management
def create_credentials(emp_id, role):
    """HR creates the first login for an existing employee. Returns the temporary password (shown once)."""
    if role not in ALLOWED_ROLES:
        raise AuthError(400, f"Role must be one of: {', '.join(ALLOWED_ROLES)}.")

    record = employee_store.find_employee_with_login(emp_id=emp_id.strip())
    if record is None:
        raise AuthError(404, "No employee found with this ID.")
    if record["login"] is not None:
        raise AuthError(409, "This employee already has a login. Use reset-password instead.")
    if not record["is_active"]:
        raise AuthError(400, "This employee is not active.")
    if not record["dob"]:
        raise AuthError(400, "This employee has no date of birth saved, so a temporary password cannot be made.")

    temporary_password = build_temporary_password(record["dob"], record["emp_id"])
    employee_store.insert_login(record["id"], hash_password(temporary_password), role)
    return {
        "emp_id": record["emp_id"],
        "full_name": record["full_name"],
        "role": role,
        "temporary_password": temporary_password,
    }


def reset_password(emp_id):
    """HR gives an employee a new temporary password (forgotten password, or a locked account)."""
    record = employee_store.find_employee_with_login(emp_id=emp_id.strip())
    if record is None:
        raise AuthError(404, "No employee found with this ID.")
    if record["login"] is None:
        raise AuthError(404, "This employee has no login yet. Use register first.")
    if not record["dob"]:
        raise AuthError(400, "This employee has no date of birth saved, so a temporary password cannot be made.")

    temporary_password = build_temporary_password(record["dob"], record["emp_id"])
    employee_store.update_login(record["id"], {
        "password_hash": hash_password(temporary_password),
        "must_change_password": True,
        "failed_login_attempts": 0,
        "locked_until": None,
        "password_changed_at": get_current_time().isoformat(),
    })
    return {
        "emp_id": record["emp_id"],
        "full_name": record["full_name"],
        "role": record["login"]["role"],
        "temporary_password": temporary_password,
    }


def change_password(employee_id, current_password, new_password):
    """The employee sets their own password. All older tokens stop working, so they log in again."""
    record = employee_store.find_employee_with_login(employee_id=employee_id)
    if record is None or record["login"] is None:
        raise AuthError(401, SESSION_EXPIRED_MESSAGE, "session_expired")

    login = record["login"]
    if is_locked(login):
        raise AuthError(429, "Too many failed attempts. Please try again in a few minutes.")
    if not verify_password(current_password, login["password_hash"]):
        record_failed_attempt(employee_id, login)
        raise AuthError(401, "Your current password is not correct.", "wrong_current_password")

    if len(new_password) < MINIMUM_NEW_PASSWORD_LENGTH:
        raise AuthError(400, f"The new password must have at least {MINIMUM_NEW_PASSWORD_LENGTH} characters.")
    if len(new_password.encode("utf-8")) > MAXIMUM_PASSWORD_BYTES:
        raise AuthError(400, f"The new password can be at most {MAXIMUM_PASSWORD_BYTES} bytes long.")
    if new_password == current_password:
        raise AuthError(400, "The new password must be different from the current one.")

    employee_store.update_login(employee_id, {
        "password_hash": hash_password(new_password),
        "must_change_password": False,
        "failed_login_attempts": 0,
        "locked_until": None,
        "password_changed_at": get_current_time().isoformat(),
    })
