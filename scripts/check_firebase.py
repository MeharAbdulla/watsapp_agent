"""
Test Firebase connection. Run after placing service account JSON in project folder.

  py scripts/check_firebase.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))


def main():
    print("Firebase configuration check\n")
    use = (os.getenv("USE_FIREBASE") or "true").strip()
    cred = (os.getenv("FIREBASE_CREDENTIALS") or "").strip()
    project = (os.getenv("FIREBASE_PROJECT_ID") or "").strip()

    print(f"  USE_FIREBASE          = {use}")
    print(f"  FIREBASE_CREDENTIALS  = {cred or '(not set)'}")
    print(f"  FIREBASE_PROJECT_ID   = {project or '(optional — read from JSON)'}")

    from firebase_store import _resolve_credentials_path, init_firebase, is_active

    if not cred:
        print("\n[FAIL] Set FIREBASE_CREDENTIALS in .env")
        print("       Example: FIREBASE_CREDENTIALS=firebase-service-account.json")
        return 1

    try:
        path = _resolve_credentials_path()
        print(f"\n  Credentials file: {path}")
    except FileNotFoundError as err:
        print(f"\n[FAIL] {err}")
        print("\nSteps:")
        print("  1. Firebase Console > Project settings > Service accounts")
        print("  2. Generate new private key > save as firebase-service-account.json")
        print(f"  3. Put the file in: {ROOT}")
        return 1

    try:
        init_firebase()
        assert is_active()
        from firebase_store import _require_db

        db = _require_db()
        # Light read — proves Firestore API works
        list(db.collection("tenants").limit(1).stream())
        print("\n[OK] Firebase connected — Firestore is ready.")
        return 0
    except Exception as err:
        print(f"\n[FAIL] {err}")
        print("\nEnable Firestore: Firebase Console → Build → Firestore → Create database")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
