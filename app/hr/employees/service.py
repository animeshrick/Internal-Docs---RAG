"""
hr/employees/service.py - adding a brand-new employee.

This only creates the employee ROW (name, email, department, ...). It does not create a
login: HR still calls /register afterwards (app/identity/auth.py's create_credentials),
the same as for an employee who was already in the table.
"""
from app.hr.employees import store as employee_store
from app.identity.auth import AuthError


def create_employee(fields):
    """HR only. Adds a new employee row. Raises AuthError (409) if the emp_id or email is already used."""
    emp_id = fields["emp_id"].strip()
    full_name = fields["full_name"].strip()
    email = fields["email"].strip()
    if not emp_id or not full_name or not email:
        raise AuthError(400, "Give the employee ID, full name and email.")

    if employee_store.find_employee_with_login(emp_id=emp_id) is not None:
        raise AuthError(409, "This employee ID is already used.")
    if employee_store.find_employee_by_email(email) is not None:
        raise AuthError(409, "This email is already used.")

    row = employee_store.insert_employee({
        "emp_id": emp_id,
        "full_name": full_name,
        "email": email,
        "dept": fields.get("dept") or None,
        "phone": fields.get("phone") or None,
        "gender": fields.get("gender") or None,
        "dob": fields["date_of_birth"].isoformat() if fields.get("date_of_birth") else None,
        "doj": fields["date_of_joining"].isoformat() if fields.get("date_of_joining") else None,
        "salary": fields.get("salary"),
        "is_confidential": bool(fields.get("is_confidential", False)),
    })
    return {
        "emp_id": row["emp_id"], "full_name": row["full_name"], "email": row["email"], "dept": row.get("dept"),
    }
