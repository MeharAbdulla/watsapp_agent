"""
One-time master admin bootstrap.

Smart behavior:
  - If no account exists with the given email -> creates a new admin account.
  - If an account exists with the given email -> promotes it to admin
    (keeps its existing API key, name, tenant data, etc.).

After this runs successfully you should never need it again — manage all
users (admins or normal) from the Admin Dashboard in the web UI.

Usage:
    py setup_admin.py "Master Admin" "admin@yourcompany.com" "yourPassword"

Notes:
  - The password is only used when CREATING a brand new account. If the
    email already exists, the password argument is ignored (the existing
    account keeps its current password).
"""

import os
import sys

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from api.database import create_tenant, promote_email_to_admin
from firebase_store import ensure_firebase, get_tenant_by_email, is_active


def main():
    args = sys.argv[1:]
    if len(args) < 3:
        print('Usage: py setup_admin.py "Master Admin" "admin@example.com" "password"')
        sys.exit(1)

    name, email, password = args[0], args[1], args[2]

    if len(password) < 6:
        print("Error: Password must be at least 6 characters.")
        sys.exit(1)

    if not is_active():
        try:
            ensure_firebase()
        except Exception as err:
            print(f"Error initializing Firebase: {err}")
            sys.exit(1)

    existing = None
    try:
        existing = get_tenant_by_email(email)
    except Exception as err:
        print(f"Warning: could not look up existing accounts ({err}).")

    if existing:
        if existing.get("is_admin"):
            print(f"Account '{email}' is already an admin. Nothing to do.")
            print(f"  Name:  {existing.get('name')}")
            print(f"  Role:  Admin")
            return

        try:
            promoted = promote_email_to_admin(email)
        except Exception as err:
            print(f"Error promoting account: {err}")
            sys.exit(1)

        if not promoted:
            print(f"Could not promote '{email}' — account not found after lookup. Try again.")
            sys.exit(1)

        print("\nExisting account promoted to admin.")
        print(f"  Name:  {promoted.get('name')}")
        print(f"  Email: {promoted.get('email')}")
        print(f"  Role:  Admin")
        print("\nSign in at the dashboard with this account's existing password.")
        print("(The password argument was ignored because the account already exists.)")
        return

    try:
        tenant = create_tenant(name, email, password, is_admin=True)
        print("\nMaster admin account created.")
        print(f"  Name:  {tenant['name']}")
        print(f"  Email: {tenant['email']}")
        print(f"  Role:  Admin")
        print("\nSign in at the dashboard with this email + password.")
        print("All future users (admin or normal) should be created from the Admin Dashboard UI.")
    except ValueError as err:
        print(f"Error: {err}")
        sys.exit(1)
    except Exception as err:
        print(f"Unexpected error: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
