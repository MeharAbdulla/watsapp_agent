"""
Firebase Firestore — primary storage for tenants, flow state, RAG chunks, PDF extracts.

.env:
  USE_FIREBASE=true
  FIREBASE_CREDENTIALS=firebase-service-account.json
  FIREBASE_PROJECT_ID=your-project-id
"""

import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime

_firebase_ready = False
_db = None

TENANTS_COLLECTION = "tenants"
CONVERSATION_COLLECTION = "conversation_states"
CHAT_HISTORY_COLLECTION = "chat_history"
EXTRACTED_COLLECTION = "extracted_documents"
KNOWLEDGE_COLLECTION = "tenant_knowledge"


def firebase_required() -> bool:
    flag = (os.getenv("USE_FIREBASE") or "true").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _project_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _find_credentials_file() -> str | None:
    """Resolve service account JSON from .env path or auto-detect download name."""
    import glob as glob_module

    base = _project_root()
    cred = (os.getenv("FIREBASE_CREDENTIALS") or "").strip()
    candidates = []
    if cred:
        candidates.append(cred)
        if not os.path.isabs(cred):
            candidates.append(os.path.join(base, cred))
    candidates.append(os.path.join(base, "firebase-service-account.json"))
    for path in sorted(glob_module.glob(os.path.join(base, "*firebase-adminsdk*.json"))):
        candidates.append(path)

    seen = set()
    for path in candidates:
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isfile(norm) and not norm.endswith(".example.json"):
            return norm
    return None


def use_firebase() -> bool:
    if not firebase_required():
        return False
    return _find_credentials_file() is not None


def _resolve_credentials_path() -> str:
    path = _find_credentials_file()
    if path:
        return path
    cred = (os.getenv("FIREBASE_CREDENTIALS") or "").strip()
    base = _project_root()
    raise FileNotFoundError(
        f"Firebase credentials not found. Download JSON from Firebase Console and save as:\n"
        f"  {os.path.join(base, cred or 'firebase-service-account.json')}\n"
        f"Or place any *-firebase-adminsdk-*.json file in {base}"
    )


def init_firebase() -> bool:
    global _firebase_ready, _db
    if _firebase_ready:
        return True
    if not use_firebase():
        return False

    import firebase_admin
    from firebase_admin import credentials, firestore

    if not firebase_admin._apps:
        import json

        cred_path = _resolve_credentials_path()
        cred = credentials.Certificate(cred_path)
        options = {}
        project_id = (os.getenv("FIREBASE_PROJECT_ID") or "").strip()
        if not project_id:
            try:
                with open(cred_path, "r", encoding="utf-8") as handle:
                    project_id = (json.load(handle).get("project_id") or "").strip()
            except (OSError, json.JSONDecodeError):
                pass
        if project_id:
            options["projectId"] = project_id
        firebase_admin.initialize_app(cred, options or None)

    _db = firestore.client()
    _firebase_ready = True
    return True


def ensure_firebase():
    """Raise if Firebase is required but not configured."""
    if not firebase_required():
        return False
    if not use_firebase():
        raise RuntimeError(
            "Firebase is required (USE_FIREBASE=true). "
            "Set FIREBASE_CREDENTIALS to your service account JSON path in .env"
        )
    if not init_firebase():
        raise RuntimeError("Firebase failed to initialize")
    return True


def is_active() -> bool:
    return use_firebase() and init_firebase()


def _require_db():
    ensure_firebase()
    return _db


def init_db():
    if use_firebase():
        init_firebase()


# --- Tenants ---


def hash_password(password: str) -> str:
    import hashlib
    # Simple unsalted SHA256 for this minimal SaaS
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_tenant(name: str, email: str, password: str, is_admin: bool = False) -> dict:
    db = _require_db()
    tenant_id = str(uuid.uuid4())
    api_key = secrets.token_urlsafe(32)
    created = datetime.utcnow().isoformat()
    email_norm = email.lower().strip()

    existing = (
        db.collection(TENANTS_COLLECTION)
        .where("email", "==", email_norm)
        .limit(1)
        .stream()
    )
    if any(True for _ in existing):
        raise ValueError("Email already registered")

    payload = {
        "name": name,
        "email": email_norm,
        "api_key": api_key,
        "password_hash": hash_password(password),
        "is_admin": bool(is_admin),
        "created_at": created,
    }
    db.collection(TENANTS_COLLECTION).document(tenant_id).set(payload)
    return {"id": tenant_id, **payload}


def list_all_tenants() -> list:
    db = _require_db()
    rows = []
    for doc in db.collection(TENANTS_COLLECTION).stream():
        data = doc.to_dict() or {}
        rows.append({
            "id": doc.id,
            "name": data.get("name", ""),
            "email": data.get("email", ""),
            "is_admin": bool(data.get("is_admin", False)),
            "created_at": data.get("created_at", ""),
        })
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def delete_tenant(tenant_id: str) -> bool:
    db = _require_db()
    ref = db.collection(TENANTS_COLLECTION).document(tenant_id)
    snap = ref.get()
    if not snap.exists:
        return False
    ref.delete()
    return True


def any_admin_exists() -> bool:
    db = _require_db()
    docs = (
        db.collection(TENANTS_COLLECTION)
        .where("is_admin", "==", True)
        .limit(1)
        .stream()
    )
    return any(True for _ in docs)


def set_tenant_admin(tenant_id: str, is_admin: bool) -> bool:
    db = _require_db()
    ref = db.collection(TENANTS_COLLECTION).document(tenant_id)
    snap = ref.get()
    if not snap.exists:
        return False
    ref.update({"is_admin": bool(is_admin)})
    return True


def promote_email_to_admin(email: str) -> dict | None:
    db = _require_db()
    email_norm = (email or "").lower().strip()
    if not email_norm:
        return None
    for doc in (
        db.collection(TENANTS_COLLECTION)
        .where("email", "==", email_norm)
        .limit(1)
        .stream()
    ):
        doc.reference.update({"is_admin": True})
        data = doc.to_dict() or {}
        data["id"] = doc.id
        data["is_admin"] = True
        return data
    return None


def get_tenant_by_api_key(api_key: str):
    db = _require_db()
    for doc in (
        db.collection(TENANTS_COLLECTION)
        .where("api_key", "==", api_key)
        .limit(1)
        .stream()
    ):
        return {"id": doc.id, **doc.to_dict()}
    return None


def get_tenant_by_email(email: str):
    db = _require_db()
    email_norm = email.lower().strip()
    for doc in (
        db.collection(TENANTS_COLLECTION)
        .where("email", "==", email_norm)
        .limit(1)
        .stream()
    ):
        return {"id": doc.id, **doc.to_dict()}
    return None


# --- Conversation flow state ---


def load_conversation_states(tenant_id: str) -> dict:
    db = _require_db()
    doc = db.collection(CONVERSATION_COLLECTION).document(tenant_id).get()
    if doc.exists:
        return dict((doc.to_dict() or {}).get("states") or {})
    return {}


def save_conversation_states(tenant_id: str, states: dict):
    db = _require_db()
    db.collection(CONVERSATION_COLLECTION).document(tenant_id).set(
        {"states": states, "updated_at": datetime.utcnow().isoformat()},
        merge=True,
    )


# --- Chat history ---


def _safe_doc_key(value: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", (value or "").strip())[:80]
    return slug or "unknown"


def _rag_doc_id(tenant_id: str, chat_key: str) -> str:
    return f"{tenant_id}__{_safe_doc_key(chat_key)}"


def load_chat_history(tenant_id: str, chat_key: str) -> list:
    db = _require_db()
    doc = db.collection(CHAT_HISTORY_COLLECTION).document(_rag_doc_id(tenant_id, chat_key)).get()
    if doc.exists:
        return list((doc.to_dict() or {}).get("history") or [])
    return []


def save_chat_history(tenant_id: str, chat_key: str, history: list):
    db = _require_db()
    db.collection(CHAT_HISTORY_COLLECTION).document(_rag_doc_id(tenant_id, chat_key)).set(
        {
            "tenant_id": tenant_id,
            "chat_key": chat_key,
            "history": history,
            "updated_at": datetime.utcnow().isoformat(),
        },
        merge=True,
    )


# --- PDF / extracted text ---


def save_extracted_document(
    tenant_id: str,
    source_filename: str,
    full_text: str,
    pages: int = 0,
    label: str = "",
):
    db = _require_db()
    digest = hashlib.sha256(f"{tenant_id}:{source_filename}".encode()).hexdigest()[:24]
    db.collection(EXTRACTED_COLLECTION).document(digest).set(
        {
            "tenant_id": tenant_id,
            "source_filename": source_filename,
            "label": label,
            "pages": pages,
            "char_count": len(full_text),
            "text": full_text,
            "extracted_at": datetime.utcnow().isoformat(),
        },
        merge=True,
    )
    return digest


def list_extracted_documents(tenant_id: str, limit: int = 20) -> list:
    db = _require_db()
    docs = (
        db.collection(EXTRACTED_COLLECTION)
        .where("tenant_id", "==", tenant_id)
        .limit(limit)
        .stream()
    )
    rows = []
    for doc in docs:
        data = doc.to_dict() or {}
        rows.append(
            {
                "id": doc.id,
                "source_filename": data.get("source_filename"),
                "char_count": data.get("char_count"),
                "extracted_at": data.get("extracted_at"),
            }
        )
    return rows


# --- Tenant knowledge (services list text) ---


def load_tenant_services_text(tenant_id: str) -> str | None:
    db = _require_db()
    doc = db.collection(KNOWLEDGE_COLLECTION).document(tenant_id).get()
    if doc.exists:
        text = (doc.to_dict() or {}).get("services_text")
        if text and str(text).strip():
            return str(text)
    return None


def save_tenant_services_text(tenant_id: str, services_text: str):
    db = _require_db()
    db.collection(KNOWLEDGE_COLLECTION).document(tenant_id).set(
        {
            "services_text": services_text,
            "updated_at": datetime.utcnow().isoformat(),
        },
        merge=True,
    )


def load_agent_config(tenant_id: str) -> dict:
    db = _require_db()
    doc = db.collection(KNOWLEDGE_COLLECTION).document(tenant_id).get()
    if not doc.exists:
        return {}
    data = doc.to_dict() or {}
    return {
        "bot_mode": data.get("bot_mode"),
        "typing_profile": data.get("typing_profile"),
        "embedding_model": data.get("embedding_model"),
        "business_name": data.get("business_name"),
        "messages": data.get("messages") or {},
        "extra_knowledge": data.get("extra_knowledge", ""),
    }


def save_agent_config(tenant_id: str, config: dict):
    db = _require_db()
    payload = {
        "bot_mode": config.get("bot_mode", "gemini"),
        "typing_profile": config.get("typing_profile", "slow"),
        "embedding_model": config.get("embedding_model", "all-MiniLM-L6-v2"),
        "business_name": config.get("business_name", ""),
        "messages": config.get("messages") or {},
        "extra_knowledge": config.get("extra_knowledge", ""),
        "updated_at": datetime.utcnow().isoformat(),
    }
    if config.get("services_text") is not None:
        payload["services_text"] = config["services_text"]
    db.collection(KNOWLEDGE_COLLECTION).document(tenant_id).set(payload, merge=True)
