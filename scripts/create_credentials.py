"""
create_credentials.py - create logins from the command line (for the first HR account and for existing staff).

One employee (use this first to make the first HR account):
    python scripts/create_credentials.py EMP001 hr

Every active employee that has no login yet (role employee). The passwords are saved in a CSV file
in data/backup/ (ignored by git). Hand them out, then DELETE the file.
    python scripts/create_credentials.py --all
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "app"))

import auth  # noqa: E402
import employee_store  # noqa: E402


def create_for_one_employee(emp_id, role):
    result = auth.create_credentials(emp_id, role)
    print(f"Login created for {result['full_name']} ({result['emp_id']}), role {result['role']}.")
    print(f"Temporary password (shown only once): {result['temporary_password']}")


def create_for_all_employees():
    employees = employee_store.list_employees_without_login()
    if not employees:
        print("Every active employee already has a login.")
        return

    created_rows = []
    for employee in employees:
        try:
            result = auth.create_credentials(employee["emp_id"], "employee")
            created_rows.append(result)
        except auth.AuthError as error:
            # Example: the employee has no date of birth saved.
            print(f"Skipped {employee['emp_id']}: {error.message}")

    if created_rows:
        output_folder = PROJECT_ROOT / "data" / "backup"
        output_folder.mkdir(parents=True, exist_ok=True)
        output_path = output_folder / f"new_credentials_{datetime.now():%Y%m%d_%H%M%S}.csv"
        with open(output_path, "w", newline="", encoding="utf8") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=["emp_id", "full_name", "role", "temporary_password"])
            writer.writeheader()
            writer.writerows(created_rows)
        print(f"Created {len(created_rows)} logins. Passwords saved in: {output_path}")
        print("Hand them out, then delete that file.")


def main():
    parser = argparse.ArgumentParser(description="Create employee logins.")
    parser.add_argument("emp_id", nargs="?", help="employee id, for example EMP001")
    parser.add_argument("role", nargs="?", default="employee", choices=auth.ALLOWED_ROLES)
    parser.add_argument("--all", action="store_true", help="create logins for every active employee without one")
    arguments = parser.parse_args()

    if arguments.all:
        create_for_all_employees()
    elif arguments.emp_id:
        create_for_one_employee(arguments.emp_id, arguments.role)
    else:
        parser.error("Give an emp_id, or use --all.")


if __name__ == "__main__":
    main()
