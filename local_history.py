import os
import json
from datetime import datetime
from tenant_paths import ensure_tenant_layout

def _chat_history_path(tenant_id: str, chat_name: str) -> str:
    import re
    slug = re.sub(r"[^\w\-]+", "_", chat_name.strip())[:60] or "unknown_contact"
    if tenant_id == "default":
        base = os.path.dirname(os.path.abspath(__file__))
        path_dir = os.path.join(base, "chat_data", slug)
    else:
        paths = ensure_tenant_layout(tenant_id)
        path_dir = os.path.join(paths["root"], "chat_data", slug)
    os.makedirs(path_dir, exist_ok=True)
    return os.path.join(path_dir, "history.json")

def load_chat_history(tenant_id: str, chat_name: str) -> list:
    try:
        from firebase_store import is_active, load_chat_history as fb_load
        if is_active():
            return fb_load(tenant_id, chat_name)
    except ImportError:
        pass
        
    path = _chat_history_path(tenant_id, chat_name)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return []

def save_chat_history(tenant_id: str, chat_name: str, history: list):
    try:
        from firebase_store import is_active, save_chat_history as fb_save
        if is_active():
            fb_save(tenant_id, chat_name, history)
            return
    except ImportError:
        pass
        
    path = _chat_history_path(tenant_id, chat_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def ingest_message(tenant_id: str, chat_name: str, text: str, role: str = "client"):
    history = load_chat_history(tenant_id, chat_name)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    history.append({
        "role": role,
        "text": text,
        "indexed_at": datetime.utcnow().isoformat(),
        "source": f"{role}_{stamp}"
    })
    # Keep last 50
    if len(history) > 50:
        history = history[-50:]
    save_chat_history(tenant_id, chat_name, history)
