"""An in-memory stand-in for the leave tables, so the leave rules can be tested without Supabase.
Call FakeDatabase().install(monkeypatch) and every leave_store function reads and writes this fake instead."""
from datetime import date, datetime, timedelta, timezone

from app.hr.leave import store as leave_store


class FakeDatabase:
    def __init__(self):
        self.employees = {
            1: {"id": 1, "emp_id": "EMP001", "full_name": "Priya Sen", "dept": "HR", "gender": "Female", "manager_id": None, "is_active": True},
            2: {"id": 2, "emp_id": "EMP002", "full_name": "Rahul Verma", "dept": "IT", "gender": "Male", "manager_id": None, "is_active": True},
            3: {"id": 3, "emp_id": "EMP003", "full_name": "Ananya Das", "dept": "IT", "gender": "Female", "manager_id": 2, "is_active": True},
            4: {"id": 4, "emp_id": "EMP004", "full_name": "Karan Mehta", "dept": "IT", "gender": "Male", "manager_id": 2, "is_active": True},
            5: {"id": 5, "emp_id": "EMP005", "full_name": "Neha Kapoor", "dept": "Sales", "gender": "Female", "manager_id": 1, "is_active": True},
            6: {"id": 6, "emp_id": "EMP006", "full_name": "Sam Roy", "dept": "pm", "gender": "Male", "manager_id": None, "is_active": True},
            7: {"id": 7, "emp_id": "EMP007", "full_name": "Dev Paul", "dept": "Design", "gender": "Male", "manager_id": None, "is_active": True},
        }
        self.leave_types = {
            "casual": {"code": "casual", "name": "Casual Leave", "days_allowed": 8, "limit_type": "per_year", "count_mode": "working_days",
                       "applies_to_gender": None, "approver": "manager_or_hr", "can_apply_by_chat": True, "max_days_per_request": 7},
            "sick": {"code": "sick", "name": "Sick Leave", "days_allowed": 12, "limit_type": "per_year", "count_mode": "working_days",
                     "applies_to_gender": None, "approver": "manager_or_hr", "can_apply_by_chat": True, "max_days_per_request": 7},
            "maternity": {"code": "maternity", "name": "Maternity Leave", "days_allowed": 90, "limit_type": "per_request",
                          "count_mode": "calendar_days", "applies_to_gender": "Female", "approver": "hr_only",
                          "can_apply_by_chat": False, "max_days_per_request": None},
        }
        self.holidays = {date(2026, 12, 25)}
        self.leave_requests = []
        self.actions = []
        self.next_id = 1

    def install(self, monkeypatch):
        for name in ("get_employee_row get_employees_by_ids list_direct_report_ids list_unmanaged_employee_ids list_active_employees_in_department get_leave_types get_leave_balances count_leave_days "
                     "find_overlapping_requests insert_leave_request get_leave_request list_leave_requests list_pending_requests "
                     "update_leave_request get_waiting_action get_action_by_id update_action create_pending_action").split():
            monkeypatch.setattr(leave_store, name, getattr(self, name))
        return self

    def new_id(self):
        self.next_id += 1
        return self.next_id - 1

    def add_leave(self, employee_id, leave_type, start, end, days, status="approved", day_type="full", document_path=None):
        row = {"id": self.new_id(), "employee_id": employee_id, "leave_type": leave_type, "start_date": start, "end_date": end,
               "day_type": day_type, "days": days, "reason": None, "status": status, "document_path": document_path,
               "approver_id": None, "decided_at": None, "decision_note": None, "created_at": "2026-11-01T10:00:00+00:00"}
        self.leave_requests.append(row)
        return row

    # ---- employees
    def get_employee_row(self, employee_id):
        return self.employees.get(employee_id)

    def get_employees_by_ids(self, employee_ids):
        return {i: self.employees[i] for i in employee_ids if i in self.employees}

    def list_unmanaged_employee_ids(self):
        return [e["id"] for e in self.employees.values() if e["manager_id"] is None]

    def list_active_employees_in_department(self, department_name):
        return [e for e in self.employees.values() if e["dept"].lower() == department_name.lower() and e["is_active"]]

    def list_direct_report_ids(self, manager_id):
        return [e["id"] for e in self.employees.values() if e["manager_id"] == manager_id]

    # ---- types, counting, balances
    def get_leave_types(self):
        return self.leave_types

    def count_leave_days(self, start_date, end_date, day_type, count_mode):
        if day_type == "half":
            return 0.5 if count_mode == "calendar_days" or (start_date.isoweekday() < 6 and start_date not in self.holidays) else 0.0
        days = [start_date + timedelta(n) for n in range((end_date - start_date).days + 1)]
        if count_mode == "calendar_days":
            return float(len(days))
        return float(len([d for d in days if d.isoweekday() < 6 and d not in self.holidays]))

    def get_leave_balances(self, employee_id, on_date):
        rows = []
        year_start = date(on_date.year if on_date.month >= 4 else on_date.year - 1, 4, 1)       # the leave year starts on 1 April
        year_end = date(year_start.year + 1, 4, 1)
        for leave_type in self.leave_types.values():
            mine = [r for r in self.leave_requests if r["employee_id"] == employee_id and r["leave_type"] == leave_type["code"]
                    and r["status"] in ("approved", "pending") and year_start <= date.fromisoformat(r["start_date"]) < year_end]
            used = sum(float(r["days"]) for r in mine if r["status"] == "approved")
            pending = sum(float(r["days"]) for r in mine if r["status"] == "pending")
            rows.append({"type_code": leave_type["code"], "type_name": leave_type["name"], "limit_type": leave_type["limit_type"],
                         "days_allowed": leave_type["days_allowed"], "used_days": used, "pending_days": pending,
                         "remaining_days": leave_type["days_allowed"] - used - pending if leave_type["limit_type"] == "per_year" else None,
                         "max_days_per_request": leave_type["max_days_per_request"], "can_apply_by_chat": leave_type["can_apply_by_chat"]})
        return rows

    # ---- requests
    def find_overlapping_requests(self, employee_id, start_date, end_date):
        return [r for r in self.leave_requests if r["employee_id"] == employee_id and r["status"] in ("pending", "approved")
                and r["start_date"] <= end_date.isoformat() and r["end_date"] >= start_date.isoformat()]

    def insert_leave_request(self, fields):
        row = self.add_leave(fields["employee_id"], fields["leave_type"], fields["start_date"], fields["end_date"], fields["days"],
                             status="pending", day_type=fields["day_type"], document_path=fields.get("document_path"))
        row["reason"] = fields.get("reason")
        return row

    def get_leave_request(self, leave_id):
        return next((r for r in self.leave_requests if r["id"] == leave_id), None)

    def list_leave_requests(self, employee_id, status=None, limit=20):
        rows = [r for r in self.leave_requests if r["employee_id"] == employee_id and (status is None or r["status"] == status)]
        return sorted(rows, key=lambda r: r["start_date"], reverse=True)[:limit]

    def list_pending_requests(self, employee_ids=None):
        return [r for r in self.leave_requests if r["status"] == "pending" and (employee_ids is None or r["employee_id"] in employee_ids)]

    def update_leave_request(self, leave_id, fields, expected_status):
        row = self.get_leave_request(leave_id)
        if row is None or row["status"] != expected_status:
            return None
        row.update(fields)
        return row

    # ---- pending actions
    def get_waiting_action(self, employee_id):
        return next((a for a in self.actions if a["employee_id"] == employee_id and a["status"] == "waiting"), None)

    def get_action_by_id(self, action_id):
        return next((a for a in self.actions if a["id"] == action_id), None)

    def update_action(self, action_id, fields, expected_status="waiting"):
        action = self.get_action_by_id(action_id)
        if action is None or action["status"] != expected_status:
            return None
        action.update(fields)
        return action

    def create_pending_action(self, employee_id, action_type, payload, summary):
        old = self.get_waiting_action(employee_id)
        if old:
            old["status"] = "cancelled"
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        action = {"id": self.new_id(), "employee_id": employee_id, "action_type": action_type, "payload": payload,
                  "summary": summary, "status": "waiting", "expires_at": expires}
        self.actions.append(action)
        return action
