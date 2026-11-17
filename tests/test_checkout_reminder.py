"""Tests for the 7:30pm "do you want to check out?" reminder job (app/hr/attendance/checkout_reminder.py)."""
import pytest

from app.chat import store as chat_store
from app.hr.attendance import checkout_reminder
from app.hr.leave import store as leave_store
from fakes_attendance import FakeAttendanceDatabase

TODAY = "2026-11-12"


@pytest.fixture
def database(monkeypatch):
    fake = FakeAttendanceDatabase().install(monkeypatch)
    fake.set_time("19:30", TODAY)
    return fake


class CreatedActions(list):
    """Same as a plain list of the actions this job created, plus .busy - the set of employee ids a
    test can mark as "already waiting on something else", so get_waiting_action reports them as busy."""
    def __init__(self):
        super().__init__()
        self.busy = set()


@pytest.fixture
def actions(monkeypatch):
    """A tiny stand-in for the pending_actions table - just enough to check what this job creates."""
    created = CreatedActions()

    def get_waiting_action(employee_id):
        return {"id": 1} if employee_id in created.busy else None

    def create_pending_action(employee_id, action_type, payload, summary, expires_at=None):
        row = {"employee_id": employee_id, "action_type": action_type, "payload": payload, "summary": summary, "expires_at": expires_at}
        created.append(row)
        return row

    monkeypatch.setattr(leave_store, "get_waiting_action", get_waiting_action)
    monkeypatch.setattr(leave_store, "create_pending_action", create_pending_action)
    return created


@pytest.fixture
def messages(monkeypatch):
    saved = []
    monkeypatch.setattr(chat_store, "save_assistant_message", lambda employee_id, text, created_at: saved.append((employee_id, text)))
    return saved


def test_only_checked_in_and_not_checked_out_people_are_asked(database, actions, messages):
    database.insert_attendance({"employee_id": 3, "work_date": TODAY, "check_in_at": "2026-11-12T04:30:00+00:00"})        # still checked in
    database.insert_attendance({"employee_id": 4, "work_date": TODAY, "check_in_at": "2026-11-12T04:30:00+00:00",
                                "check_out_at": "2026-11-12T12:30:00+00:00"})                                            # already checked out
    # employee 1 never checked in today at all - nothing to ask them either.
    checkout_reminder.run_checkout_reminder_job()
    assert {employee_id for employee_id, _ in messages} == {3}
    assert len(actions) == 1 and actions[0]["employee_id"] == 3


def test_checking_out_already_means_no_prompt_is_shown(database, actions, messages):
    database.insert_attendance({"employee_id": 3, "work_date": TODAY, "check_in_at": "2026-11-12T04:30:00+00:00",
                                "check_out_at": "2026-11-12T12:30:00+00:00"})
    checkout_reminder.run_checkout_reminder_job()
    assert messages == []
    assert actions == []


def test_the_ceo_the_cto_and_the_super_admin_are_never_asked(database, actions, messages):
    for employee_id in (5, 6, 8):           # Cleo CEO, Tia CTO (confidential), Sue Super
        database.insert_attendance({"employee_id": employee_id, "work_date": TODAY, "check_in_at": "2026-11-12T04:30:00+00:00"})
    checkout_reminder.run_checkout_reminder_job()
    assert messages == []
    assert actions == []


def test_someone_already_mid_chat_is_left_alone(database, actions, messages):
    database.insert_attendance({"employee_id": 3, "work_date": TODAY, "check_in_at": "2026-11-12T04:30:00+00:00"})
    actions.busy.add(3)
    checkout_reminder.run_checkout_reminder_job()
    assert messages == []
    assert actions == []


def test_the_prompt_text_and_its_end_of_day_expiry(database, actions, messages):
    database.insert_attendance({"employee_id": 3, "work_date": TODAY, "check_in_at": "2026-11-12T04:30:00+00:00"})
    checkout_reminder.run_checkout_reminder_job()
    assert messages[0] == (3, checkout_reminder.CHECKOUT_PROMPT_TEXT)
    assert actions[0]["expires_at"] == "2026-11-13T00:00:00+05:30"       # midnight tonight, not the usual 10 minutes
