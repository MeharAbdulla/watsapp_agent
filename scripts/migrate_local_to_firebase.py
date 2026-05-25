"""
One-time migration: SQLite tenants + local JSON → Firebase Firestore.

Run after FIREBASE_CREDENTIALS is set in .env:
  py scripts/migrate_local_to_firebase.py
"""

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

from firebase_store import (
    create_tenant,
    ensure_firebase,
    get_tenant_by_email,
    load_conversation_states,
    save_conversation_states,
    save_rag_chunks,
)


def migrate_tenants():
    db_path = os.path.join(ROOT, "saas.db")
    if not os.path.isfile(db_path):
        print("No saas.db — skip tenants")
        return
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tenants").fetchall()
    conn.close()
    for row in rows:
        email = row["email"]
        if get_tenant_by_email(email):
            print(f"  tenant exists: {email}")
            continue
        print(f"  migrating tenant: {email}")
        # Manual insert preserves id if we extend firebase_store — skip for safety
        create_tenant(row["name"], email)


def migrate_conversation_states():
    for path in _walk("conversation_states.json"):
        tenant_hint = _tenant_from_path(path)
        with open(path, "r", encoding="utf-8") as handle:
            states = json.load(handle)
        if states:
            existing = load_conversation_states(tenant_hint)
            existing.update(states)
            save_conversation_states(tenant_hint, existing)
            print(f"  conversation_states → {tenant_hint} ({len(states)} chats)")


def migrate_rag_chunks():
    for path in _walk("chunks.json"):
        tenant_hint = _tenant_from_path(path)
        chat_dir = os.path.basename(os.path.dirname(path))
        with open(path, "r", encoding="utf-8") as handle:
            chunks = json.load(handle)
        if chunks:
            save_rag_chunks(tenant_hint, chat_dir, chunks)
            print(f"  rag_chunks → {tenant_hint}/{chat_dir} ({len(chunks)} chunks)")


def _walk(filename):
    for root, _dirs, files in os.walk(ROOT):
        if "tenants" in root.replace("\\", "/").split("/") or root == ROOT:
            if filename in files:
                yield os.path.join(root, filename)


def _tenant_from_path(path):
    parts = path.replace("\\", "/").split("/")
    if "tenants" in parts:
        idx = parts.index("tenants")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "default"


def main():
    ensure_firebase()
    print("Migrating local data to Firebase...")
    migrate_tenants()
    migrate_conversation_states()
    migrate_rag_chunks()
    print("Done.")


if __name__ == "__main__":
    main()
