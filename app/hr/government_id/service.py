"""
hr/government_id/service.py - the mandatory government ID (Aadhaar or PAN) every employee must give.

The rules:
    - Each employee submits exactly ONE government ID: either Aadhaar or PAN, their choice.
    - Aadhaar is a two-sided card, so it needs a front AND a back image.
      PAN is used front-only, so it needs the front image only.
    - Until the ID is on file the employee can log in, change their password and submit the ID,
      and nothing else: the gate in main.py (get_ready_employee) refuses every other endpoint.
      The moment the ID is saved, the same session continues into the app; no second login.
    - Submitting is a one-time action. Nobody can replace their own ID afterwards. Only the super
      admin can clear it, which asks that employee for a new one at their next login.
    - The number itself is encrypted before it reaches the database (app/core/encryption.py).
      The employee sees only the masked form ("Aadhaar ending 1234"); the full number can be
      read by the super admin alone, and every such look-up is written to the audit log.
    - The super admin account (EMP-ADMIN) is not a person, so it never has to submit anything.
"""
import logging
import uuid

from app.core import encryption
from app.hr.attendance import store as attendance_store
from app.hr.employees import store as employee_store
from app.hr.government_id import store as government_id_store
from app.hr.superadmin import store as superadmin_store

logger = logging.getLogger(__name__)

AADHAAR = "aadhaar"
PAN = "pan"
ID_TYPES = (AADHAAR, PAN)

ID_TYPE_NAMES = {AADHAAR: "Aadhaar", PAN: "PAN"}

AADHAAR_DIGITS = 12
PAN_LENGTH = 10
LAST4_LENGTH = 4


class GovernmentIdError(Exception):
    """A problem the API should show to the client, with the HTTP status code to use."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ------------------------------------------------------------------ the Aadhaar checksum
# An Aadhaar number carries a Verhoeff check digit: the last digit is calculated from the first
# eleven. A typed number that fails this check is a typing mistake, not a real Aadhaar number.
# The two tables below are the Verhoeff algorithm's own tables; they are not ours to change.
VERHOEFF_MULTIPLICATION = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
VERHOEFF_PERMUTATION = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def has_valid_aadhaar_checksum(digits_text):
    """True when the 12 digits end in the right Verhoeff check digit."""
    checksum = 0
    for position, character in enumerate(reversed(digits_text)):
        checksum = VERHOEFF_MULTIPLICATION[checksum][VERHOEFF_PERMUTATION[position % 8][int(character)]]
    return checksum == 0


# ------------------------------------------------------------------ the number itself
def clean_number(id_type, raw_number):
    """Remove the spaces and dashes people type, and upper-case a PAN. Does not check it yet."""
    text = "".join((raw_number or "").split())
    text = text.replace("-", "")
    return text.upper() if id_type == PAN else text


def check_id_type(id_type):
    if id_type not in ID_TYPES:
        raise GovernmentIdError("Choose either Aadhaar or PAN.")


def check_number(id_type, number):
    """Raise GovernmentIdError when the number cannot be a real Aadhaar / PAN number."""
    check_id_type(id_type)
    if id_type == AADHAAR:
        if not (len(number) == AADHAAR_DIGITS and number.isdigit()):
            raise GovernmentIdError("An Aadhaar number has 12 digits.")
        if number[0] in ("0", "1"):
            raise GovernmentIdError("An Aadhaar number does not start with 0 or 1. Please check the number.")
        if not has_valid_aadhaar_checksum(number):
            raise GovernmentIdError("That Aadhaar number is not valid. Please check the digits and try again.")
        return number

    # PAN: five letters, four digits, one letter. For example ABCDE1234F.
    if len(number) != PAN_LENGTH:
        raise GovernmentIdError("A PAN number has 10 characters, like ABCDE1234F.")
    letters, digits, last_letter = number[:5], number[5:9], number[9:]
    if not (letters.isalpha() and digits.isdigit() and last_letter.isalpha()):
        raise GovernmentIdError("A PAN number looks like ABCDE1234F: five letters, four digits, then one letter.")
    return number


def mask_number(id_type, last4):
    """What the employee and the lists are allowed to see: "Aadhaar ending 1234"."""
    return f"{ID_TYPE_NAMES.get(id_type, 'ID')} ending {last4}"


# ------------------------------------------------------------------ who has to submit
def is_exempt(employee):
    """The super admin account is an admin entity, not a person, so it submits nothing."""
    return (employee or {}).get("role") == "superadmin"


def needs_submission(employee):
    """True when this employee must submit their government ID before using the rest of the app."""
    if is_exempt(employee):
        return False
    return not employee.get("has_government_id", False)


def describe_own(employee):
    """What GET /me/government-id answers: the employee's own ID, number masked, never in full."""
    if is_exempt(employee):
        return {"submitted": False, "required": False, "id_type": None, "masked_number": None, "submitted_at": None}

    row = government_id_store.get_government_id(employee["employee_id"])
    if row is None:
        return {"submitted": False, "required": True, "id_type": None, "masked_number": None, "submitted_at": None}
    return {
        "submitted": True,
        "required": True,
        "id_type": row["id_type"],
        "masked_number": mask_number(row["id_type"], row["id_number_last4"]),
        "submitted_at": row["submitted_at"],
    }


# The upload endpoint names every stored file after the content type it checked, so the type can be
# read back from the path later and no client has to be trusted to report it.
IMAGE_EXTENSION_BY_CONTENT_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}
CONTENT_TYPE_BY_IMAGE_EXTENSION = {extension: content_type for content_type, extension in IMAGE_EXTENSION_BY_CONTENT_TYPE.items()}


def build_image_path(employee_id, side, content_type):
    """Where one side of an ID is stored: "<employee id>/<random>-front.jpg". The folder is the
    employee's own id, which is also how we check later that a submitted path belongs to them."""
    extension = IMAGE_EXTENSION_BY_CONTENT_TYPE[content_type]
    return f"{employee_id}/{uuid.uuid4().hex}-{side}{extension}"


def content_type_for_path(image_path):
    """Read the content type back from a stored path. None when the path has no extension we wrote."""
    if not image_path:
        return None
    dot_position = image_path.rfind(".")
    return CONTENT_TYPE_BY_IMAGE_EXTENSION.get(image_path[dot_position:].lower()) if dot_position != -1 else None


def check_images(id_type, front_image_path, back_image_path):
    """Aadhaar needs both sides; PAN needs the front only."""
    if not front_image_path:
        raise GovernmentIdError("Please upload the front image of your ID.")
    if id_type == AADHAAR and not back_image_path:
        raise GovernmentIdError("Aadhaar needs both sides. Please upload the back image too.")
    if id_type == PAN and back_image_path:
        raise GovernmentIdError("PAN needs the front image only.")


def check_image_belongs_to_employee(employee_id, image_path):
    """
    An uploaded file is stored in a folder named after the employee who uploaded it, so a submitted
    path that points anywhere else is refused. Without this check, someone could submit the path of
    another person's uploaded image.
    """
    if not image_path:
        return
    if not image_path.startswith(f"{employee_id}/") or content_type_for_path(image_path) is None:
        raise GovernmentIdError("That uploaded image could not be found. Please upload the image again.")


def submit(employee, id_type, raw_number, front_image_path, back_image_path=None):
    """Save this employee's one government ID. Refuses when they already have one on file."""
    if is_exempt(employee):
        raise GovernmentIdError("The super admin account does not submit a government ID.", 403)

    check_id_type(id_type)
    if government_id_store.get_government_id(employee["employee_id"]) is not None:
        raise GovernmentIdError(
            "Your government ID is already on file. Ask the super admin if it has to be changed.", 409)

    number = check_number(id_type, clean_number(id_type, raw_number))
    check_images(id_type, front_image_path, back_image_path)
    check_image_belongs_to_employee(employee["employee_id"], front_image_path)
    check_image_belongs_to_employee(employee["employee_id"], back_image_path)
    front_content_type = content_type_for_path(front_image_path)
    back_content_type = content_type_for_path(back_image_path)

    government_id_store.insert_government_id({
        "employee_id": employee["employee_id"],
        "id_type": id_type,
        "id_number_encrypted": encryption.encrypt_text(number),
        "id_number_last4": number[-LAST4_LENGTH:],
        "front_image_path": front_image_path,
        "back_image_path": back_image_path,
        "front_content_type": front_content_type,
        "back_content_type": back_content_type,
    })
    logger.info("Government ID (%s) submitted by %s", id_type, employee["emp_id"])
    return {
        "submitted": True,
        "id_type": id_type,
        "masked_number": mask_number(id_type, number[-LAST4_LENGTH:]),
        "message": "Your government ID is saved. Thank you.",
    }


# ------------------------------------------------------------------ the super admin's side
def require_superadmin(admin):
    if (admin or {}).get("role") != "superadmin":
        raise GovernmentIdError("Only the super admin can do this.", 403)


def find_employee(emp_id_text):
    record = employee_store.find_employee_with_login(emp_id=(emp_id_text or "").strip())
    if record is None:
        raise GovernmentIdError("No employee found with this ID.", 404)
    return record


def write_audit_entry(admin, target, action, details):
    """Looking at or clearing somebody's ID is recorded, like every other super admin action."""
    try:
        superadmin_store.insert_audit_entry({
            "actor_id": admin["employee_id"], "actor_emp_id": admin["emp_id"],
            "target_id": target["id"], "target_emp_id": target["emp_id"],
            "action": action, "details": details,
        })
    except Exception:
        logger.exception("Could not save the super admin audit entry for %s on %s", action, target["emp_id"])


def list_submission_states(admin):
    """
    Super admin only: every active employee and whether their ID is on file, the number masked.
    One read of the people and one read of the ID table, then matched up here - never one database
    call per person. The super admin account itself is left out; it is exempt.
    """
    require_superadmin(admin)
    submitted_by_employee_id = government_id_store.list_submitted_employee_ids()
    rows = []
    for person in attendance_store.list_people():
        if person.get("role") == "superadmin":
            continue
        submitted = submitted_by_employee_id.get(person["id"])
        rows.append({
            "emp_id": person["emp_id"],
            "full_name": person["full_name"],
            "dept": person.get("dept"),
            "role": person.get("role") or "employee",
            "submitted": submitted is not None,
            "id_type": submitted["id_type"] if submitted else None,
            "masked_number": mask_number(submitted["id_type"], submitted["id_number_last4"]) if submitted else None,
            "submitted_at": submitted["submitted_at"] if submitted else None,
        })
    return rows


def read_full_id(admin, emp_id_text):
    """Super admin only: one employee's ID type and FULL number. The look-up itself is audited."""
    require_superadmin(admin)
    target = find_employee(emp_id_text)

    row = government_id_store.get_government_id(target["id"], include_number=True)
    if row is None:
        raise GovernmentIdError("This employee has not submitted their government ID yet.", 404)

    try:
        full_number = encryption.decrypt_text(row["id_number_encrypted"])
    except encryption.EncryptionError:
        # The key in .env no longer matches what the number was stored with.
        raise GovernmentIdError(
            "This number cannot be read with the current encryption key. Clear the ID so the employee submits it again.",
            500)

    write_audit_entry(admin, target, "government_id_viewed", {"id_type": row["id_type"]})
    return {
        "emp_id": target["emp_id"],
        "full_name": target["full_name"],
        "id_type": row["id_type"],
        "id_number": full_number,
        "masked_number": mask_number(row["id_type"], row["id_number_last4"]),
        "submitted_at": row["submitted_at"],
        "front_image_path": row["front_image_path"],
        "back_image_path": row["back_image_path"],
    }


def get_image_path(admin, emp_id_text, side):
    """Super admin only: the stored path of one side, so main.py can make a short-lived link for it."""
    require_superadmin(admin)
    if side not in ("front", "back"):
        raise GovernmentIdError("Ask for the front or the back image.")
    target = find_employee(emp_id_text)

    row = government_id_store.get_government_id(target["id"])
    if row is None:
        raise GovernmentIdError("This employee has not submitted their government ID yet.", 404)

    path = row["front_image_path"] if side == "front" else row["back_image_path"]
    if not path:
        raise GovernmentIdError("There is no back image for this ID (a PAN card is stored front-only).", 404)
    return path


def clear(admin, emp_id_text):
    """
    Super admin only: remove one employee's ID so they are asked for it again at their next login.
    The stored images are left in the private bucket; only the super admin can ever reach them,
    and keeping them means a cleared ID can still be checked afterwards if a question comes up.
    """
    require_superadmin(admin)
    target = find_employee(emp_id_text)

    row = government_id_store.get_government_id(target["id"])
    if row is None:
        raise GovernmentIdError("This employee has not submitted their government ID yet.", 404)

    government_id_store.delete_government_id(target["id"])
    write_audit_entry(admin, target, "government_id_cleared", {"id_type": row["id_type"], "ended_with": row["id_number_last4"]})
    logger.info("Government ID of %s cleared by %s", target["emp_id"], admin["emp_id"])
    return {"emp_id": target["emp_id"], "full_name": target["full_name"],
            "message": f"{target['full_name']} will be asked for a new government ID at their next login."}
