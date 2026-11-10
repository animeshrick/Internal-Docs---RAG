"""
reset_password.py - give an employee who already has a login a NEW temporary password.

Use it for a forgotten password or a locked account. The role does not change.
The employee must change the password at the next login. The password is shown only once.

    python scripts/reset_password.py EMP-1001
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.identity import auth  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Give an employee a new temporary password.")
    parser.add_argument("emp_id", help="employee id, for example EMP-1001")
    arguments = parser.parse_args()

    try:
        result = auth.reset_password(arguments.emp_id)
    except auth.AuthError as error:
        sys.exit(f"Could not reset: {error.message}")
    print(f"New temporary password for {result['full_name']} ({result['emp_id']}), role {result['role']}.")
    print(f"Temporary password (shown only once): {result['temporary_password']}")


if __name__ == "__main__":
    main()
