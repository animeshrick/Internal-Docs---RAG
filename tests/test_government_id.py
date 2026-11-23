"""
Tests for the mandatory government ID (Aadhaar / PAN): the number rules, who still has to submit,
the one-time submit, and the super admin's read / clear. No real Supabase or Storage calls are made.
"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.hr.government_id import service as government_id_service
from app.hr.government_id.service import GovernmentIdError

client = TestClient(main.app)

TOKEN_HEADERS = {"Authorization": "Bearer any-token"}

# A valid Aadhaar number: 12 digits that end in the right Verhoeff check digit.
VALID_AADHAAR = "234567890124"
VALID_PAN = "ABCDE1234F"


def make_employee(employee_id=3, role="employee", has_government_id=False, must_change_password=False):
    return {"employee_id": employee_id, "emp_id": f"EMP00{employee_id}", "full_name": "Ananya Das", "dept": "IT",
            "role": role, "must_change_password": must_change_password, "has_government_id": has_government_id}


class FakeGovernmentIdStore:
    """An in-memory stand-in for the employee_government_id table."""

    def __init__(self):
        self.rows = {}

    def install(self, monkeypatch):
        monkeypatch.setattr(government_id_service.government_id_store, "get_government_id", self.get_government_id)
        monkeypatch.setattr(government_id_service.government_id_store, "insert_government_id", self.insert_government_id)
        monkeypatch.setattr(government_id_service.government_id_store, "delete_government_id", self.delete_government_id)
        monkeypatch.setattr(government_id_service.government_id_store, "list_submitted_employee_ids", lambda: dict(self.rows))
        return self

    def get_government_id(self, employee_id, include_number=False):
        row = self.rows.get(employee_id)
        if row is None:
            return None
        row = dict(row)
        if not include_number:
            row.pop("id_number_encrypted", None)
        return row

    def insert_government_id(self, fields):
        row = dict(fields, submitted_at="2026-11-23T10:00:00+00:00")
        self.rows[fields["employee_id"]] = row
        return row

    def delete_government_id(self, employee_id):
        self.rows.pop(employee_id, None)


@pytest.fixture
def fake_store(monkeypatch):
    """An empty ID table, plus encryption that does not need a real key in .env."""
    monkeypatch.setattr(government_id_service.encryption, "encrypt_text", lambda text: f"encrypted:{text}")
    monkeypatch.setattr(government_id_service.encryption, "decrypt_text",
                        lambda text: text.split("encrypted:", 1)[1] if text.startswith("encrypted:") else _raise_key_error())
    return FakeGovernmentIdStore().install(monkeypatch)


def _raise_key_error():
    raise government_id_service.encryption.EncryptionError("wrong key")


@pytest.fixture
def audit_entries(monkeypatch):
    """Collects what would have been written to the super admin audit log."""
    entries = []
    monkeypatch.setattr(government_id_service.superadmin_store, "insert_audit_entry", entries.append)
    return entries


@pytest.fixture
def logged_in(monkeypatch):
    """Makes every request look like it comes from a valid token. Change .employee to test the gate."""
    class LoggedIn:
        employee = make_employee()

    holder = LoggedIn()
    monkeypatch.setattr(main.auth, "load_employee_from_token", lambda token: holder.employee)
    return holder


def get_error(function, *arguments, **keyword_arguments):
    with pytest.raises(GovernmentIdError) as raised:
        function(*arguments, **keyword_arguments)
    return raised.value


# ------------------------------------------------------------------ the number rules
def test_a_valid_aadhaar_and_pan_are_accepted():
    assert government_id_service.check_number("aadhaar", VALID_AADHAAR) == VALID_AADHAAR
    assert government_id_service.check_number("pan", VALID_PAN) == VALID_PAN


def test_spaces_dashes_and_lower_case_are_cleaned_up():
    assert government_id_service.clean_number("aadhaar", " 2345 6789 0124 ") == VALID_AADHAAR
    assert government_id_service.clean_number("aadhaar", "2345-6789-0124") == VALID_AADHAAR
    assert government_id_service.clean_number("pan", "abcde1234f") == VALID_PAN


@pytest.mark.parametrize("number", ["23456789012", "2345678901234", "23456789012a"])
def test_an_aadhaar_number_must_be_twelve_digits(number):
    assert "12 digits" in get_error(government_id_service.check_number, "aadhaar", number).message


@pytest.mark.parametrize("number", ["023456789012", "123456789012"])
def test_an_aadhaar_number_cannot_start_with_zero_or_one(number):
    assert "0 or 1" in get_error(government_id_service.check_number, "aadhaar", number).message


def test_a_mistyped_aadhaar_number_fails_the_checksum():
    assert "not valid" in get_error(government_id_service.check_number, "aadhaar", "234567890123").message


@pytest.mark.parametrize("number", ["ABCDE1234", "ABCD12345F", "ABCDE12345", "12345ABCDF"])
def test_a_pan_number_must_be_five_letters_four_digits_and_a_letter(number):
    assert get_error(government_id_service.check_number, "pan", number).status_code == 400


def test_an_unknown_id_type_is_refused():
    assert "Aadhaar or PAN" in get_error(government_id_service.check_number, "voter", "123").message


def test_the_masked_number_shows_only_the_last_four_characters():
    assert government_id_service.mask_number("aadhaar", "0124") == "Aadhaar ending 0124"
    assert government_id_service.mask_number("pan", "234F") == "PAN ending 234F"


# ------------------------------------------------------------------ who still has to submit
def test_an_employee_without_an_id_must_submit():
    assert government_id_service.needs_submission(make_employee(has_government_id=False)) is True


def test_an_employee_with_an_id_does_not_have_to_submit_again():
    assert government_id_service.needs_submission(make_employee(has_government_id=True)) is False


def test_the_super_admin_account_is_never_asked():
    admin = make_employee(employee_id=8, role="superadmin", has_government_id=False)
    assert government_id_service.is_exempt(admin) is True
    assert government_id_service.needs_submission(admin) is False


def test_the_ceo_and_everyone_else_is_asked_like_a_normal_employee():
    for role in ("employee", "hr", "pm", "ceo"):
        assert government_id_service.needs_submission(make_employee(role=role, has_government_id=False)) is True


# ------------------------------------------------------------------ submitting
def test_aadhaar_is_saved_with_both_sides_and_the_number_is_encrypted(fake_store):
    employee = make_employee()
    result = government_id_service.submit(employee, "aadhaar", " 2345 6789 0124 ",
                                          "3/aaa-front.jpg", "3/bbb-back.png")

    assert result["masked_number"] == "Aadhaar ending 0124"
    saved = fake_store.rows[3]
    assert saved["id_number_encrypted"] == f"encrypted:{VALID_AADHAAR}"
    assert saved["id_number_last4"] == "0124"
    assert saved["front_content_type"] == "image/jpeg"
    assert saved["back_content_type"] == "image/png"
    # The full number is never stored in plain text anywhere in the row.
    assert VALID_AADHAAR not in str({key: value for key, value in saved.items() if key != "id_number_encrypted"})


def test_pan_is_saved_with_the_front_image_only(fake_store):
    government_id_service.submit(make_employee(), "pan", "abcde1234f", "3/aaa-front.pdf")

    saved = fake_store.rows[3]
    assert saved["id_type"] == "pan"
    assert saved["back_image_path"] is None
    assert saved["back_content_type"] is None
    assert saved["front_content_type"] == "application/pdf"


def test_aadhaar_without_the_back_image_is_refused(fake_store):
    error = get_error(government_id_service.submit, make_employee(), "aadhaar", VALID_AADHAAR, "3/aaa-front.jpg")
    assert "both sides" in error.message
    assert fake_store.rows == {}


def test_pan_with_a_back_image_is_refused(fake_store):
    error = get_error(government_id_service.submit, make_employee(), "pan", VALID_PAN,
                      "3/aaa-front.jpg", "3/bbb-back.jpg")
    assert "front image only" in error.message


def test_the_front_image_is_required(fake_store):
    assert "front image" in get_error(government_id_service.submit, make_employee(), "pan", VALID_PAN, "").message


def test_another_persons_uploaded_image_cannot_be_submitted(fake_store):
    """The folder in the path is the uploader's own employee id, so a path from somebody else is refused."""
    error = get_error(government_id_service.submit, make_employee(), "pan", VALID_PAN, "99/aaa-front.jpg")
    assert "upload the image again" in error.message
    assert fake_store.rows == {}


def test_an_image_path_with_an_unknown_extension_is_refused(fake_store):
    assert get_error(government_id_service.submit, make_employee(), "pan", VALID_PAN, "3/aaa-front.exe").status_code == 400


def test_an_id_cannot_be_replaced_once_it_is_on_file(fake_store):
    government_id_service.submit(make_employee(), "pan", VALID_PAN, "3/aaa-front.jpg")
    error = get_error(government_id_service.submit, make_employee(), "aadhaar", VALID_AADHAAR,
                      "3/ccc-front.jpg", "3/ddd-back.jpg")
    assert error.status_code == 409
    assert fake_store.rows[3]["id_type"] == "pan"      # the first one is untouched


def test_the_super_admin_account_cannot_submit_one(fake_store):
    admin = make_employee(employee_id=8, role="superadmin")
    assert get_error(government_id_service.submit, admin, "pan", VALID_PAN, "8/aaa-front.jpg").status_code == 403


def test_my_own_id_is_described_with_the_number_masked(fake_store):
    employee = make_employee()
    assert government_id_service.describe_own(employee) == {
        "submitted": False, "required": True, "id_type": None, "masked_number": None, "submitted_at": None}

    government_id_service.submit(employee, "aadhaar", VALID_AADHAAR, "3/a-front.jpg", "3/b-back.jpg")
    described = government_id_service.describe_own(employee)
    assert described["submitted"] is True
    assert described["masked_number"] == "Aadhaar ending 0124"
    assert "id_number" not in described


# ------------------------------------------------------------------ the super admin's side
@pytest.fixture
def people(monkeypatch):
    """Two employees and the super admin, as the employee lookups see them."""
    everyone = [
        {"id": 3, "emp_id": "EMP003", "full_name": "Ananya Das", "dept": "IT", "role": "employee", "is_active": True},
        {"id": 4, "emp_id": "EMP004", "full_name": "Karan Mehta", "dept": "IT", "role": "employee", "is_active": True},
        {"id": 8, "emp_id": "EMP-ADMIN", "full_name": "Admin", "dept": "Management", "role": "superadmin", "is_active": True},
    ]
    monkeypatch.setattr(government_id_service.attendance_store, "list_people", lambda: [dict(p) for p in everyone])

    def fake_find(emp_id=None, employee_id=None):
        for person in everyone:
            if person["emp_id"] == emp_id or person["id"] == employee_id:
                return dict(person, login={"role": person["role"]})
        return None

    monkeypatch.setattr(government_id_service.employee_store, "find_employee_with_login", fake_find)
    return everyone


ADMIN = {"employee_id": 8, "emp_id": "EMP-ADMIN", "full_name": "Admin", "dept": "Management",
         "role": "superadmin", "must_change_password": False, "has_government_id": True}


def test_only_the_super_admin_may_read_a_full_number(fake_store, people):
    for role in ("employee", "hr", "pm", "ceo"):
        error = get_error(government_id_service.read_full_id, make_employee(role=role), "EMP003")
        assert error.status_code == 403


def test_the_super_admin_reads_the_full_number_and_the_read_is_audited(fake_store, people, audit_entries):
    government_id_service.submit(make_employee(), "aadhaar", VALID_AADHAAR, "3/a-front.jpg", "3/b-back.jpg")

    result = government_id_service.read_full_id(ADMIN, "EMP003")
    assert result["id_number"] == VALID_AADHAAR
    assert result["masked_number"] == "Aadhaar ending 0124"
    assert result["full_name"] == "Ananya Das"

    assert len(audit_entries) == 1
    assert audit_entries[0]["action"] == "government_id_viewed"
    assert audit_entries[0]["actor_emp_id"] == "EMP-ADMIN"
    assert audit_entries[0]["target_emp_id"] == "EMP003"
    # The number itself is not copied into the audit log.
    assert VALID_AADHAAR not in str(audit_entries[0])


def test_reading_the_id_of_somebody_who_has_not_submitted_says_so(fake_store, people):
    assert get_error(government_id_service.read_full_id, ADMIN, "EMP004").status_code == 404


def test_reading_the_id_of_an_unknown_employee_says_so(fake_store, people):
    assert get_error(government_id_service.read_full_id, ADMIN, "EMP999").status_code == 404


def test_a_number_stored_with_another_key_gives_a_clear_error(fake_store, people, monkeypatch):
    government_id_service.submit(make_employee(), "pan", VALID_PAN, "3/a-front.jpg")
    monkeypatch.setattr(government_id_service.encryption, "decrypt_text", lambda text: _raise_key_error())

    error = get_error(government_id_service.read_full_id, ADMIN, "EMP003")
    assert error.status_code == 500
    assert "encryption key" in error.message


def test_the_image_path_of_each_side_is_found(fake_store, people):
    government_id_service.submit(make_employee(), "aadhaar", VALID_AADHAAR, "3/a-front.jpg", "3/b-back.png")
    assert government_id_service.get_image_path(ADMIN, "EMP003", "front") == "3/a-front.jpg"
    assert government_id_service.get_image_path(ADMIN, "EMP003", "back") == "3/b-back.png"


def test_a_pan_has_no_back_image_to_show(fake_store, people):
    government_id_service.submit(make_employee(), "pan", VALID_PAN, "3/a-front.jpg")
    assert get_error(government_id_service.get_image_path, ADMIN, "EMP003", "back").status_code == 404


def test_only_the_super_admin_may_ask_for_an_image(fake_store, people):
    assert get_error(government_id_service.get_image_path, make_employee(role="hr"), "EMP003", "front").status_code == 403


def test_clearing_an_id_removes_it_and_is_audited(fake_store, people, audit_entries):
    government_id_service.submit(make_employee(), "aadhaar", VALID_AADHAAR, "3/a-front.jpg", "3/b-back.jpg")

    result = government_id_service.clear(ADMIN, "EMP003")
    assert "Ananya Das" in result["message"]
    assert fake_store.rows == {}
    assert audit_entries[-1]["action"] == "government_id_cleared"
    assert audit_entries[-1]["details"]["ended_with"] == "0124"


def test_only_the_super_admin_may_clear_an_id(fake_store, people):
    government_id_service.submit(make_employee(), "pan", VALID_PAN, "3/a-front.jpg")
    assert get_error(government_id_service.clear, make_employee(role="hr"), "EMP003").status_code == 403
    assert fake_store.rows != {}


def test_clearing_an_id_that_is_not_there_says_so(fake_store, people):
    assert get_error(government_id_service.clear, ADMIN, "EMP003").status_code == 404


def test_the_overview_lists_everyone_and_leaves_the_super_admin_out(fake_store, people):
    government_id_service.submit(make_employee(), "pan", VALID_PAN, "3/a-front.jpg")

    rows = government_id_service.list_submission_states(ADMIN)
    assert [row["emp_id"] for row in rows] == ["EMP003", "EMP004"]
    assert rows[0]["submitted"] is True and rows[0]["masked_number"] == "PAN ending 234F"
    assert rows[1]["submitted"] is False and rows[1]["masked_number"] is None


def test_only_the_super_admin_sees_the_overview(fake_store, people):
    assert get_error(government_id_service.list_submission_states, make_employee(role="hr")).status_code == 403


# ------------------------------------------------------------------ the gate, through the API
def test_every_other_page_is_refused_until_the_id_is_submitted(logged_in):
    logged_in.employee = make_employee(has_government_id=False)
    response = client.get("/me/profile", headers=TOKEN_HEADERS)
    assert response.status_code == 403
    assert response.json()["code"] == "must_submit_government_id"


def test_the_pages_open_once_the_id_is_on_file(logged_in, monkeypatch):
    logged_in.employee = make_employee(has_government_id=True)
    monkeypatch.setattr(main.people_service, "read_own_profile", lambda employee: {
        "emp_id": "EMP003", "full_name": "Ananya Das", "role": "employee", "is_active": True})
    assert client.get("/me/profile", headers=TOKEN_HEADERS).status_code == 200


def test_me_reports_the_step_that_is_still_left(logged_in):
    logged_in.employee = make_employee(has_government_id=False)
    body = client.get("/me", headers=TOKEN_HEADERS).json()
    assert body["must_change_password"] is False
    assert body["must_submit_government_id"] is True


def test_the_temporary_password_comes_before_the_government_id(logged_in):
    """Both steps are pending: the password message is the one the employee gets first."""
    logged_in.employee = make_employee(must_change_password=True, has_government_id=False)
    response = client.get("/government-id", headers=TOKEN_HEADERS)
    assert response.status_code == 403
    assert response.json()["code"] == "must_change_password"


def test_the_government_id_page_is_open_while_the_id_is_missing(logged_in, fake_store):
    logged_in.employee = make_employee(has_government_id=False)
    response = client.get("/government-id", headers=TOKEN_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"submitted": False, "required": True, "id_type": None,
                               "masked_number": None, "submitted_at": None}


def test_submitting_through_the_api_saves_the_id(logged_in, fake_store):
    logged_in.employee = make_employee(has_government_id=False)
    response = client.post("/government-id", headers=TOKEN_HEADERS, json={
        "id_type": "aadhaar", "id_number": "2345 6789 0124",
        "front_image_path": "3/a-front.jpg", "back_image_path": "3/b-back.jpg"})

    assert response.status_code == 200
    assert response.json()["masked_number"] == "Aadhaar ending 0124"
    assert fake_store.rows[3]["id_type"] == "aadhaar"


def test_a_bad_number_through_the_api_is_a_readable_error(logged_in, fake_store):
    logged_in.employee = make_employee(has_government_id=False)
    response = client.post("/government-id", headers=TOKEN_HEADERS, json={
        "id_type": "aadhaar", "id_number": "234567890123",
        "front_image_path": "3/a-front.jpg", "back_image_path": "3/b-back.jpg"})

    assert response.status_code == 400
    assert response.json()["code"] == "government_id"
    assert "not valid" in response.json()["detail"]


def test_an_unknown_id_type_is_rejected_before_the_handler(logged_in, fake_store):
    logged_in.employee = make_employee(has_government_id=False)
    response = client.post("/government-id", headers=TOKEN_HEADERS, json={
        "id_type": "voter", "id_number": VALID_PAN, "front_image_path": "3/a-front.jpg"})
    assert response.status_code == 422


def test_an_image_upload_refuses_a_file_type_that_is_not_an_id_scan(logged_in):
    logged_in.employee = make_employee(has_government_id=False)
    response = client.post("/government-id/images?side=front", headers=dict(
        TOKEN_HEADERS, **{"Content-Type": "text/plain"}), content=b"not an image")
    assert response.status_code == 415


def test_an_image_upload_refuses_an_empty_file(logged_in, monkeypatch):
    logged_in.employee = make_employee(has_government_id=False)
    response = client.post("/government-id/images?side=front", headers=dict(
        TOKEN_HEADERS, **{"Content-Type": "image/png"}), content=b"")
    assert response.status_code == 400


def test_an_uploaded_image_is_stored_under_the_employees_own_folder(logged_in, monkeypatch):
    logged_in.employee = make_employee(has_government_id=False)
    uploads = []
    monkeypatch.setattr(main.storage_store, "upload_file",
                        lambda bucket, path, data, content_type: uploads.append((bucket, path, content_type)))

    response = client.post("/government-id/images?side=back", headers=dict(
        TOKEN_HEADERS, **{"Content-Type": "image/jpeg"}), content=b"pretend-jpeg-bytes")

    assert response.status_code == 200
    body = response.json()
    assert body["side"] == "back"
    assert body["image_path"].startswith("3/") and body["image_path"].endswith("-back.jpg")
    assert uploads[0][0] == "government-ids"


# ------------------------------------------------------------------ the shape of a Supabase answer
# PostgREST answers 300 Multiple Choices when an embedded table can be reached by more than one
# foreign key, and the body is an error object instead of the rows. "requests" counts 3xx as ok,
# so check_response has to look at the status code itself. This is what broke the login the first
# time the government ID table was created with a second foreign key to employee.
def test_a_three_hundred_from_supabase_is_an_error_not_data():
    from app.core import supabase

    class AmbiguousEmbedResponse:
        status_code = 300
        ok = True       # what requests reports for any 3xx
        text = '{"code":"PGRST201","message":"Could not embed because more than one relationship was found"}'

    with pytest.raises(RuntimeError) as raised:
        supabase.check_response(AmbiguousEmbedResponse(), "Reading the employee")
    assert "300" in str(raised.value)
    assert "PGRST201" in str(raised.value)


def test_a_normal_success_still_passes():
    from app.core import supabase

    class OkResponse:
        status_code = 200
        ok = True
        text = "[]"

    supabase.check_response(OkResponse(), "Reading the employee")      # must not raise
