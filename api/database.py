"""
Database layer — Firebase Firestore when configured.
"""

from firebase_store import (
    any_admin_exists as _fb_any_admin_exists,
    create_tenant as _fb_create_tenant,
    delete_tenant as _fb_delete_tenant,
    get_tenant_by_api_key as _fb_get_tenant_by_api_key,
    get_tenant_by_email as _fb_get_tenant_by_email,
    init_db as _fb_init_db,
    is_active,
    list_all_tenants as _fb_list_all_tenants,
    promote_email_to_admin as _fb_promote_email_to_admin,
    set_tenant_admin as _fb_set_tenant_admin,
)


def _using_firebase() -> bool:
    return is_active()


def _require_firebase():
    if not is_active():
        raise RuntimeError(
            "Firebase is not configured. Add FIREBASE_CREDENTIALS to your .env file."
        )


def init_db():
    if is_active():
        _fb_init_db()


def create_tenant(name: str, email: str, password: str, is_admin: bool = False) -> dict:
    _require_firebase()
    return _fb_create_tenant(name, email, password, is_admin=is_admin)


def get_tenant_by_api_key(api_key: str):
    if not is_active():
        return None
    return _fb_get_tenant_by_api_key(api_key)


def get_tenant_by_email(email: str):
    if not is_active():
        return None
    return _fb_get_tenant_by_email(email)


def list_all_tenants() -> list:
    _require_firebase()
    return _fb_list_all_tenants()


def delete_tenant(tenant_id: str) -> bool:
    _require_firebase()
    return _fb_delete_tenant(tenant_id)


def any_admin_exists() -> bool:
    _require_firebase()
    return _fb_any_admin_exists()


def set_tenant_admin(tenant_id: str, is_admin: bool) -> bool:
    _require_firebase()
    return _fb_set_tenant_admin(tenant_id, is_admin)


def promote_email_to_admin(email: str):
    _require_firebase()
    return _fb_promote_email_to_admin(email)
