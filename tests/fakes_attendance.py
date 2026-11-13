"""An in-memory stand-in for the attendance tables, so the attendance rules can be tested without Supabase.
Call FakeAttendanceDatabase().install(monkeypatch) and the attendance store functions read and write this fake instead."""
from datetime import datetime, timedelta, timezone

from app.hr.attendance import service as attendance_service
from app.hr.attendance import store as attendance_store
from app.hr.leave import store as leave_store
from app.hr.people import store as people_store

INDIA = timezone(timedelta(hours=5, minutes=30))

# id: (emp_id, full name, dept, login role, is_confidential)
PEOPLE = {
    1: ("EMP-1", "Hana HR", "HR", "hr", False),
    2: ("EMP-2", "Pam PM", "PM", "pm", False),
    3: ("EMP-3", "Ann Employee", "IT", "employee", False),
    4: ("EMP-4", "Sal Sales", "Sales", "employee", False),
    5: ("EMP-5", "Cleo CEO", "Management", "ceo", False),
    6: ("EMP-6", "Tia CTO", "Tech", "employee", True),
    7: ("EMP-7", "Harry HR", "HR", "hr", False),
    8: ("EMP-8", "Sue Super", "Management", "superadmin", False),
}


def india(day_text, clock_text):
    """'2026-11-12', '10:05' -> that moment as a UTC datetime."""
    return datetime.fromisoformat(f"{day_text}T{clock_text}:00").replace(tzinfo=INDIA).astimezone(timezone.utc)


def login_of(person_id):
    """The dictionary a logged-in person has (what load_employee_from_token returns)."""
    emp_id, full_name, dept, role, is_confidential = PEOPLE[person_id]
    return {"employee_id": person_id, "emp_id": emp_id, "full_name": full_name, "dept": dept, "role": role,
            "is_confidential": is_confidential, "must_change_password": False}


def row_of(person_id):
    """The employee row a store returns (with "id")."""
    emp_id, full_name, dept, role, is_confidential = PEOPLE[person_id]
    return {"id": person_id, "emp_id": emp_id, "full_name": full_name, "dept": dept, "role": role,
            "is_confidential": is_confidential, "is_active": True}


class FakeAttendanceDatabase:
    def __init__(self):
        self.settings = {"office_start": "10:00:00", "office_end": "19:00:00", "late_after": "10:45:00", "half_day_hours": 4.5,
                         "early_leave_after": "15:00:00", "network_check_enabled": False, "correction_window_days": 7}
        self.networks = []
        self.holidays = {"2026-12-25": "Christmas"}
        self.leaves = []                   # {"employee_id", "leave_type", "start_date", "end_date", "day_type"}
        self.attendance = []
        self.corrections = []
        self.next_id = 1
        self.now = india("2026-11-12", "10:05")       # a Thursday

    def set_time(self, clock_text, day_text="2026-11-12"):
        self.now = india(day_text, clock_text)

    def install(self, monkeypatch):
        monkeypatch.setattr(attendance_service, "get_now", lambda: self.now)
        for name in ("get_settings update_settings list_active_office_networks list_all_office_networks insert_office_network "
                     "update_office_network list_people get_person list_holidays list_approved_leaves "
                     "get_attendance list_attendance insert_attendance update_attendance insert_correction get_correction "
                     "list_corrections update_correction").split():
            monkeypatch.setattr(attendance_store, name, getattr(self, name))
        monkeypatch.setattr(people_store, "find_employee", self.find_employee)
        monkeypatch.setattr(leave_store, "get_leave_types", lambda: {"casual": {"name": "Casual Leave"}})
        return self

    def new_id(self):
        self.next_id += 1
        return self.next_id

    # ---- settings, people, calendar
    def get_settings(self):
        return dict(self.settings)

    def update_settings(self, fields):
        self.settings.update(fields)
        return dict(self.settings)

    def list_active_office_networks(self):
        return [{"id": n["id"], "label": n["label"], "cidr": n["cidr"]} for n in self.networks if n.get("is_active", True)]

    def list_all_office_networks(self):
        return [dict(n) for n in self.networks]

    def insert_office_network(self, fields):
        network = {"id": self.new_id(), "is_active": True, **fields}
        self.networks.append(network)
        return dict(network)

    def update_office_network(self, network_id, fields):
        network = next((n for n in self.networks if n["id"] == network_id), None)
        if network is None:
            return None
        network.update(fields)
        return dict(network)

    def list_people(self):
        return [row_of(person_id) for person_id in PEOPLE]

    def get_person(self, employee_id):
        return row_of(employee_id) if employee_id in PEOPLE else None

    def find_employee(self, text):
        for person_id, (emp_id, full_name, *_rest) in PEOPLE.items():
            if (text or "").strip() in (emp_id, full_name):
                return row_of(person_id)
        return None

    def list_holidays(self, start_date, end_date):
        return [{"holiday_date": day, "name": name} for day, name in self.holidays.items() if str(start_date) <= day <= str(end_date)]

    def list_approved_leaves(self, employee_ids, start_date, end_date):
        return [leave for leave in self.leaves if leave["employee_id"] in employee_ids
                and leave["start_date"] <= str(end_date) and leave["end_date"] >= str(start_date)]

    # ---- the daily record
    def get_attendance(self, employee_id, work_date):
        return next((dict(row) for row in self.attendance if row["employee_id"] == employee_id and row["work_date"] == str(work_date)), None)

    def list_attendance(self, employee_ids, start_date, end_date):
        return [dict(row) for row in self.attendance if row["employee_id"] in employee_ids and str(start_date) <= row["work_date"] <= str(end_date)]

    def insert_attendance(self, fields):
        if self.get_attendance(fields["employee_id"], fields["work_date"]):
            raise attendance_store.AttendanceConflictError()
        row = {"id": self.new_id(), "check_out_at": None, "check_in_ip": None, "check_out_ip": None, "is_late": False,
               "is_early_leave": False, "worked_minutes": None, "source": "self", **fields}
        self.attendance.append(row)
        return dict(row)

    def update_attendance(self, attendance_id, fields):
        row = next(item for item in self.attendance if item["id"] == attendance_id)
        row.update(fields)
        return dict(row)

    # ---- correction requests
    def insert_correction(self, fields):
        if any(c["employee_id"] == fields["employee_id"] and c["work_date"] == fields["work_date"] and c["status"] == "pending" for c in self.corrections):
            raise attendance_store.AttendanceConflictError()
        row = {"id": self.new_id(), "status": "pending", "decided_by": None, "decided_at": None, "decision_note": None,
               "created_at": self.now.isoformat(), "requested_check_in": None, "requested_check_out": None, **fields}
        self.corrections.append(row)
        return dict(row)

    def get_correction(self, correction_id):
        return next((dict(row) for row in self.corrections if row["id"] == correction_id), None)

    def list_corrections(self, employee_ids=None, status=None, limit=30):
        rows = [row for row in self.corrections if (employee_ids is None or row["employee_id"] in employee_ids) and (not status or row["status"] == status)]
        return [dict(row) for row in sorted(rows, key=lambda item: item["id"], reverse=True)][:limit]

    def update_correction(self, correction_id, fields, expected_status):
        row = next((item for item in self.corrections if item["id"] == correction_id and item["status"] == expected_status), None)
        if row is None:
            return None
        row.update(fields)
        return dict(row)
