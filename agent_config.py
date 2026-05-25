"""Default agent messages and settings; merged with per-tenant Firebase config."""

import copy
import json
import os

# Default Gemini chat model — client can override via GEMINI_CHAT_MODEL env var or dashboard
DEFAULT_GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash")

DEFAULT_MESSAGES = {
    "welcome_intro": "👋 *Welcome!* Thank you for contacting us.\n\nPlease reply with the *number* of the service you need:",
    "welcome_footer": "_Reply with a number *1*–*{max}* to select._",
    "invalid_selection": "⚠️ That option isn't valid.\n\nPlease reply with only a number:",
    "service_selected": "✅ *{service}* selected.\n\n{detail}\n\n{budget_prompt}",
    "budget_prompt": "💰 Please share your estimated *budget* (e.g. 50,000 PKR).",
    "budget_too_short": "Please send your estimated budget to continue.",
    "budget_recorded": "✅ Budget recorded: *{budget}*\n\n{details_prompt}",
    "details_prompt": "📝 Please describe your project (timeline, features, or requirements).",
    "details_too_short": "Please add a few more details about your project.",
    "order_summary": (
        "📋 *Order summary*\n"
        "• Service: *{service}*\n"
        "• Budget: *{budget}*\n"
        "• Details: {details}\n\n"
        "Reply *YES* to confirm or *NO* to cancel."
    ),
    "confirm_reminder": "Please reply *YES* to confirm or *NO* to cancel.",
    "order_confirmed": (
        "🎉 *Order confirmed!* Thank you.\n\n"
        "• Service: *{service}*\n"
        "• Budget: *{budget}*\n\n"
        "{closing}"
    ),
    "order_closing": "Our team will contact you shortly.\n_Reply MENU for a new order._",
    "order_cancelled": "No problem — starting over.\n\n",
    "order_done": "Your order is already confirmed. We will contact you soon.\n_Reply MENU to place another order._",
    "service_detail_fallback": "We provide *{service}* with a custom quote tailored to your needs.",
}

TYPING_PROFILES = {
    "slow": {
        "think_min": 1.8,
        "think_max": 3.8,
        "delay_min": 0.09,
        "delay_max": 0.24,
        "punct_delay": 0.55,
        "pre_send_pause": 0.9,
    },
    "normal": {
        "think_min": 1.0,
        "think_max": 2.2,
        "delay_min": 0.06,
        "delay_max": 0.15,
        "punct_delay": 0.35,
        "pre_send_pause": 0.5,
    },
}

DEFAULT_AGENT_CONFIG = {
    "gemini_chat_model": DEFAULT_GEMINI_CHAT_MODEL,
    "bot_mode": "gemini",
    "typing_profile": "slow",
    "business_name": "",
    "messages": copy.deepcopy(DEFAULT_MESSAGES),
    "extra_knowledge": "",
}


def merge_agent_config(stored: dict | None) -> dict:
    cfg = copy.deepcopy(DEFAULT_AGENT_CONFIG)
    if not stored:
        return cfg
    if stored.get("typing_profile") in TYPING_PROFILES:
        cfg["typing_profile"] = stored["typing_profile"]
    if stored.get("gemini_chat_model"):
        cfg["gemini_chat_model"] = stored["gemini_chat_model"]
    if stored.get("bot_mode"):
        cfg["bot_mode"] = stored["bot_mode"]
    if stored.get("business_name"):
        cfg["business_name"] = stored["business_name"]
    if stored.get("extra_knowledge") is not None:
        cfg["extra_knowledge"] = stored["extra_knowledge"]
    msgs = stored.get("messages") or {}
    for key, val in msgs.items():
        if key in cfg["messages"] and val and str(val).strip():
            cfg["messages"][key] = str(val).strip()
    return cfg


def get_typing_profile(name: str) -> dict:
    return TYPING_PROFILES.get(name, TYPING_PROFILES["slow"])


def _local_config_path(tenant_id: str) -> str:
    import os

    base = os.path.dirname(os.path.abspath(__file__))
    if tenant_id == "default":
        return os.path.join(base, "agent_config.json")
    return os.path.join(base, "tenants", tenant_id, "agent_config.json")


def load_tenant_agent_config(tenant_id: str) -> dict:
    try:
        from firebase_store import is_active, load_agent_config

        if is_active():
            return merge_agent_config(load_agent_config(tenant_id))
    except Exception:
        pass

    path = _local_config_path(tenant_id)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return merge_agent_config(json.load(handle))
        except (OSError, json.JSONDecodeError):
            pass
    return merge_agent_config(None)


def save_tenant_agent_config(tenant_id: str, config: dict):
    try:
        from firebase_store import is_active, save_agent_config

        if is_active():
            save_agent_config(tenant_id, config)
            return
    except Exception:
        pass

    path = _local_config_path(tenant_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
