"""Per-tenant isolated storage for SaaS workspaces."""

import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def tenant_root(tenant_id: str) -> str:
    path = os.path.join(BASE_DIR, "tenants", tenant_id)
    os.makedirs(path, exist_ok=True)
    return path


def ensure_tenant_layout(tenant_id: str) -> dict:
    root = tenant_root(tenant_id)
    paths = {
        "root": root,
        "downloads": os.path.join(root, "downloads"),
        "extracted": os.path.join(root, "extracted"),
        "chrome_data": os.path.join(root, "chrome-data"),
        "rag_data": os.path.join(root, "rag_data"),
        "knowledge": os.path.join(root, "knowledge"),
    }
    for key, path in paths.items():
        if key != "root":
            os.makedirs(path, exist_ok=True)

    global_kb = os.path.join(BASE_DIR, "knowledge")
    if os.path.isdir(global_kb):
        for name in os.listdir(global_kb):
            src = os.path.join(global_kb, name)
            dst = os.path.join(paths["knowledge"], name)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copy2(src, dst)
    return paths
