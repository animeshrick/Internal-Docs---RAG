"""
people_store.py - finds an employee from what HR typed (an id, a name) with all the details the HR assistant may show.
Only database access is here. Who may see whom is decided in people_service.py.
"""
import re

from app.core.supabase import read_rows
from payroll_store import flatten_role

PROFILE_COLUMNS = ("id,emp_id,full_name,email,phone,dept,dob,doj,gender,manager_id,is_active,is_confidential,"
                   "full_current_address,emergency_ph_no,employee_auth(role)")


def build_emp_id_variants(text):
    """EMP-1004, EMP1004 and emp-1004 all mean the same employee, so try the spellings people use."""
    match = re.fullmatch(r"([A-Za-z]+)[-_ ]?(\d+)", text.strip())
    if not match:
        return [text.strip()]
    letters, digits = match.group(1).upper(), match.group(2)
    return [f"{letters}-{digits}", f"{letters}{digits}"]


def find_employee(person_text):
    """Returns one employee row (with "role"), or None when there is no match or more than one."""
    text = (person_text or "").strip().replace("*", "")
    if not text:
        return None
    id_list = ",".join(f'"{variant}"' for variant in build_emp_id_variants(text))
    for column, filter_value in (("emp_id", f"in.({id_list})"), ("full_name", f"ilike.{text}")):
        rows = read_rows("employee", {"select": PROFILE_COLUMNS, column: filter_value, "limit": 2}, "Finding the employee")
        if len(rows) == 1:
            return flatten_role(rows[0])
    return None


def get_profile(employee_id):
    rows = read_rows("employee", {"select": PROFILE_COLUMNS, "id": f"eq.{employee_id}", "limit": 1}, "Reading the employee")
    return flatten_role(rows[0]) if rows else None
