"""Tests for 1-to-1 messaging (app/hr/messages/*): the people picker, conversations, a thread,
sending, the 24-hour visibility window, and the attachments endpoints in app/main.py."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import presence
from app.files import storage as storage_store
from app.hr.messages import service as messages_service
from app.hr.messages import store as messages_store
from app.hr.people import store as people_store
from app.identity.auth import AuthError, get_current_time

PEOPLE = [
    {"id": 1, "emp_id": "EMP001", "full_name": "Ann Employee", "dept": "IT", "role": "employee"},
    {"id": 2, "emp_id": "EMP002", "full_name": "Bob Employee", "dept": "IT", "role": "employee"},
    {"id": 8, "emp_id": "EMP008", "full_name": "Sue Super", "dept": "Management", "role": "superadmin"},
]


def iso(delta=timedelta()):
    return (get_current_time() + delta).isoformat()


def viewer_of(person):
    return {"employee_id": person["id"], "emp_id": person["emp_id"], "full_name": person["full_name"],
            "dept": person["dept"], "role": person["role"], "is_confidential": False, "must_change_password": False}


class FakeMessagesDatabase:
    """An in-memory stand-in for the direct_messages and direct_message_reads tables."""

    def __init__(self):
        self.messages = {}
        self.reads = {}  # (employee_id, other_employee_id) -> last_read_at
        self.next_id = 1

    def seed(self, sender_id, recipient_id, body=None, created_at=None, attachment_filename=None, attachment_path=None, attachment_content_type=None):
        message_id = self.next_id
        self.next_id += 1
        self.messages[message_id] = {
            "id": message_id, "sender_id": sender_id, "recipient_id": recipient_id, "body": body,
            "attachment_path": attachment_path, "attachment_filename": attachment_filename,
            "attachment_content_type": attachment_content_type, "created_at": created_at or iso(),
        }
        return message_id

    def _rows_since(self, since_iso):
        return [row for row in self.messages.values() if row["created_at"] >= since_iso]

    def list_messages_for(self, employee_id, since_iso):
        rows = [row for row in self._rows_since(since_iso) if row["sender_id"] == employee_id or row["recipient_id"] == employee_id]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def list_thread(self, employee_id, other_employee_id, since_iso):
        rows = [
            row for row in self._rows_since(since_iso)
            if {row["sender_id"], row["recipient_id"]} == {employee_id, other_employee_id}
        ]
        return sorted(rows, key=lambda row: row["created_at"])

    def find_message(self, message_id):
        return self.messages.get(message_id)

    def insert_message(self, fields):
        message_id = self.next_id
        self.next_id += 1
        row = {"id": message_id, "attachment_path": None, "attachment_filename": None, "attachment_content_type": None,
               "created_at": iso(), **fields}
        self.messages[message_id] = row
        return row

    def list_reads(self, employee_id):
        return [{"other_employee_id": other_id, "last_read_at": when}
                for (owner_id, other_id), when in self.reads.items() if owner_id == employee_id]

    def mark_read(self, employee_id, other_employee_id, when_iso):
        self.reads[(employee_id, other_employee_id)] = when_iso

    def install(self, monkeypatch):
        monkeypatch.setattr(messages_store, "list_messages_for", self.list_messages_for)
        monkeypatch.setattr(messages_store, "list_thread", self.list_thread)
        monkeypatch.setattr(messages_store, "find_message", self.find_message)
        monkeypatch.setattr(messages_store, "insert_message", self.insert_message)
        monkeypatch.setattr(messages_store, "list_reads", self.list_reads)
        monkeypatch.setattr(messages_store, "mark_read", self.mark_read)
        monkeypatch.setattr(people_store, "list_active_people", lambda: [dict(person) for person in PEOPLE])
        return self


@pytest.fixture
def database(monkeypatch):
    return FakeMessagesDatabase().install(monkeypatch)


# ------------------------------------------------------------------ list_people
def test_list_people_excludes_myself(database):
    result = messages_service.list_people(viewer_of(PEOPLE[0]))
    assert {person["emp_id"] for person in result} == {"EMP002", "EMP008"}


def test_list_people_includes_the_super_admin(database):
    result = messages_service.list_people(viewer_of(PEOPLE[0]))
    assert any(person["emp_id"] == "EMP008" for person in result)   # open messaging: anyone, including the super admin


# ------------------------------------------------------------------ the online dot (app/core/presence.py)
def test_list_people_marks_who_is_currently_online(database):
    presence._reset_for_tests()
    presence.mark_active(2)   # Bob is online; Sue is not
    result = {person["emp_id"]: person["is_online"] for person in messages_service.list_people(viewer_of(PEOPLE[0]))}
    assert result == {"EMP002": True, "EMP008": False}


def test_conversation_person_is_marked_online_too(database):
    presence._reset_for_tests()
    presence.mark_active(2)
    database.seed(1, 2, body="hi")
    result = messages_service.list_conversations(viewer_of(PEOPLE[0]))
    assert result[0]["person"]["is_online"] is True


def test_thread_person_is_marked_online_too(database):
    presence._reset_for_tests()
    presence.mark_active(2)
    result = messages_service.get_thread(viewer_of(PEOPLE[0]), "EMP002")
    assert result["person"]["is_online"] is True


# ------------------------------------------------------------------ list_conversations / unread_total
def test_list_conversations_groups_by_the_other_person_newest_first(database):
    database.seed(1, 2, body="hi", created_at=iso(timedelta(minutes=-10)))
    database.seed(8, 1, body="hello Ann", created_at=iso(timedelta(minutes=-5)))
    result = messages_service.list_conversations(viewer_of(PEOPLE[0]))
    assert [conversation["person"]["emp_id"] for conversation in result] == ["EMP008", "EMP002"]
    assert result[0]["last_message"] == "hello Ann"


def test_unread_count_only_counts_messages_sent_to_me_after_i_last_read(database):
    database.seed(2, 1, body="first", created_at=iso(timedelta(minutes=-30)))
    database.mark_read(1, 2, iso(timedelta(minutes=-20)))   # Ann read up to 20 minutes ago
    database.seed(2, 1, body="second", created_at=iso(timedelta(minutes=-10)))   # arrived after that
    result = messages_service.list_conversations(viewer_of(PEOPLE[0]))
    assert result[0]["unread_count"] == 1


def test_my_own_sent_messages_are_never_unread(database):
    database.seed(1, 2, body="hi")
    result = messages_service.list_conversations(viewer_of(PEOPLE[0]))
    assert result[0]["unread_count"] == 0


def test_a_message_older_than_24_hours_does_not_appear(database):
    database.seed(1, 2, body="old", created_at=iso(timedelta(hours=-25)))
    assert messages_service.list_conversations(viewer_of(PEOPLE[0])) == []


def test_unread_total_sums_every_conversation(database):
    database.seed(2, 1, body="from Bob")
    database.seed(8, 1, body="from Sue")
    assert messages_service.unread_total(viewer_of(PEOPLE[0])) == 2


def test_file_only_message_previews_as_sent_a_file(database):
    database.seed(1, 2, attachment_filename="photo.png", attachment_path="1/abc-photo.png", attachment_content_type="image/png")
    result = messages_service.list_conversations(viewer_of(PEOPLE[0]))
    assert result[0]["last_message"] == "Sent a file: photo.png"


# ------------------------------------------------------------------ get_thread
def test_get_thread_returns_messages_oldest_first_and_marks_whose_they_are(database):
    database.seed(1, 2, body="hi", created_at=iso(timedelta(minutes=-10)))
    database.seed(2, 1, body="hey back", created_at=iso(timedelta(minutes=-5)))
    thread = messages_service.get_thread(viewer_of(PEOPLE[0]), "EMP002")
    assert [message["body"] for message in thread["messages"]] == ["hi", "hey back"]
    assert [message["mine"] for message in thread["messages"]] == [True, False]
    assert thread["person"]["emp_id"] == "EMP002"


def test_get_thread_excludes_messages_older_than_24_hours(database):
    database.seed(1, 2, body="old", created_at=iso(timedelta(hours=-25)))
    thread = messages_service.get_thread(viewer_of(PEOPLE[0]), "EMP002")
    assert thread["messages"] == []


def test_get_thread_marks_the_conversation_read(database):
    database.seed(2, 1, body="hi")
    messages_service.get_thread(viewer_of(PEOPLE[0]), "EMP002")
    assert database.reads[(1, 2)] is not None


def test_get_thread_with_an_unknown_person_is_404(database):
    with pytest.raises(AuthError) as error:
        messages_service.get_thread(viewer_of(PEOPLE[0]), "EMP999")
    assert error.value.status_code == 404


def test_get_thread_with_myself_is_400(database):
    with pytest.raises(AuthError) as error:
        messages_service.get_thread(viewer_of(PEOPLE[0]), "EMP001")
    assert error.value.status_code == 400


# ------------------------------------------------------------------ send_message
def test_send_message_saves_the_text(database):
    saved = messages_service.send_message(viewer_of(PEOPLE[0]), "EMP002", "Hello Bob")
    assert saved["body"] == "Hello Bob"
    assert saved["mine"] is True
    assert database.messages[saved["id"]]["sender_id"] == 1
    assert database.messages[saved["id"]]["recipient_id"] == 2


def test_send_message_with_an_attachment(database):
    saved = messages_service.send_message(
        viewer_of(PEOPLE[0]), "EMP002", None,
        attachment={"path": "1/abc-note.pdf", "filename": "note.pdf", "content_type": "application/pdf"},
    )
    assert saved["attachment_filename"] == "note.pdf"
    assert saved["body"] is None


def test_send_message_with_neither_text_nor_attachment_is_400(database):
    with pytest.raises(AuthError) as error:
        messages_service.send_message(viewer_of(PEOPLE[0]), "EMP002", "   ")
    assert error.value.status_code == 400


def test_send_message_too_long_is_400(database):
    with pytest.raises(AuthError) as error:
        messages_service.send_message(viewer_of(PEOPLE[0]), "EMP002", "x" * 2001)
    assert error.value.status_code == 400


def test_send_message_to_myself_is_400(database):
    with pytest.raises(AuthError) as error:
        messages_service.send_message(viewer_of(PEOPLE[0]), "EMP001", "hi")
    assert error.value.status_code == 400


def test_send_message_to_an_unknown_person_is_404(database):
    with pytest.raises(AuthError) as error:
        messages_service.send_message(viewer_of(PEOPLE[0]), "EMP999", "hi")
    assert error.value.status_code == 404


# ------------------------------------------------------------------ check_can_read_attachment
def test_check_can_read_attachment_for_the_sender_and_the_recipient(database):
    message_id = database.seed(1, 2, attachment_path="1/abc.pdf", attachment_filename="a.pdf")
    assert messages_service.check_can_read_attachment(viewer_of(PEOPLE[0]), message_id) == "1/abc.pdf"
    assert messages_service.check_can_read_attachment(viewer_of(PEOPLE[1]), message_id) == "1/abc.pdf"


def test_check_can_read_attachment_refuses_an_outsider(database):
    message_id = database.seed(1, 2, attachment_path="1/abc.pdf", attachment_filename="a.pdf")
    with pytest.raises(AuthError) as error:
        messages_service.check_can_read_attachment(viewer_of(PEOPLE[2]), message_id)
    assert error.value.status_code == 403


def test_check_can_read_attachment_with_no_attachment_is_404(database):
    message_id = database.seed(1, 2, body="just text")
    with pytest.raises(AuthError) as error:
        messages_service.check_can_read_attachment(viewer_of(PEOPLE[0]), message_id)
    assert error.value.status_code == 404


def test_check_can_read_attachment_outside_the_window_is_404(database):
    message_id = database.seed(1, 2, attachment_path="1/abc.pdf", attachment_filename="a.pdf", created_at=iso(timedelta(hours=-25)))
    with pytest.raises(AuthError) as error:
        messages_service.check_can_read_attachment(viewer_of(PEOPLE[0]), message_id)
    assert error.value.status_code == 404


def test_check_can_read_attachment_unknown_message_is_404(database):
    with pytest.raises(AuthError) as error:
        messages_service.check_can_read_attachment(viewer_of(PEOPLE[0]), 999)
    assert error.value.status_code == 404


# ------------------------------------------------------------------ endpoints
client = TestClient(main.app)
HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
def api(database, monkeypatch):
    class Holder:
        employee = viewer_of(PEOPLE[0])

    holder = Holder()
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: {
        "employee_id": holder.employee["employee_id"], "emp_id": holder.employee["emp_id"],
        "full_name": holder.employee["full_name"], "dept": holder.employee["dept"],
        "is_confidential": False, "role": holder.employee["role"], "must_change_password": False,
    })
    return holder


def test_people_endpoint(api):
    response = client.get("/messages/people", headers=HEADERS)
    assert response.status_code == 200
    assert {person["emp_id"] for person in response.json()} == {"EMP002", "EMP008"}


def test_any_authenticated_request_marks_the_caller_online(api):
    # Ann (EMP001, the "api" fixture's logged-in employee) calling ANY endpoint should mark her
    # online - get_current_employee() in app/main.py does this for every authenticated request,
    # not just the messages ones, so a plain GET /messages/people from her is enough to prove it.
    presence._reset_for_tests()
    assert presence.is_online(PEOPLE[0]["id"]) is False
    client.get("/messages/people", headers=HEADERS)
    assert presence.is_online(PEOPLE[0]["id"]) is True


def test_conversations_endpoint(api, database):
    database.seed(2, 1, body="hi Ann")
    response = client.get("/messages/conversations", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()[0]["unread_count"] == 1


def test_thread_endpoint(api, database):
    database.seed(1, 2, body="hi")
    response = client.get("/messages/thread/EMP002", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["messages"][0]["body"] == "hi"


def test_thread_endpoint_unknown_person_is_404(api):
    response = client.get("/messages/thread/EMP999", headers=HEADERS)
    assert response.status_code == 404


def test_send_endpoint(api, database):
    response = client.post("/messages/send", json={"to_emp_id": "EMP002", "body": "Hello Bob"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["body"] == "Hello Bob"


def test_send_endpoint_rejects_an_empty_message(api):
    response = client.post("/messages/send", json={"to_emp_id": "EMP002"}, headers=HEADERS)
    assert response.status_code == 400


def test_attachment_upload_endpoint(api, monkeypatch):
    monkeypatch.setattr(storage_store, "upload_file", lambda *args, **kwargs: None)
    response = client.post(
        "/messages/attachments?filename=note.pdf", headers={**HEADERS, "Content-Type": "application/pdf"}, content=b"%PDF-1.4",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["attachment_filename"] == "note.pdf"
    assert body["attachment_path"].startswith("1/")


def test_attachment_upload_endpoint_rejects_a_bad_type(api):
    response = client.post(
        "/messages/attachments?filename=virus.exe", headers={**HEADERS, "Content-Type": "application/x-msdownload"}, content=b"x",
    )
    assert response.status_code == 415


def test_download_url_endpoint(api, database, monkeypatch):
    message_id = database.seed(1, 2, attachment_path="1/abc.pdf", attachment_filename="a.pdf")
    monkeypatch.setattr(storage_store, "create_signed_link", lambda bucket, path: f"https://signed.example/{path}")
    response = client.get(f"/messages/attachments/{message_id}/download-url", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["url"] == "https://signed.example/1/abc.pdf"


def test_download_url_endpoint_refuses_an_outsider(api, database):
    message_id = database.seed(2, 8, attachment_path="2/abc.pdf", attachment_filename="a.pdf")   # neither party is EMP001
    response = client.get(f"/messages/attachments/{message_id}/download-url", headers=HEADERS)
    assert response.status_code == 403
