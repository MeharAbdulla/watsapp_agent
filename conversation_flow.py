"""
Guided WhatsApp sales flow + FAISS RAG for service matching and details.

Steps: menu → select service → budget → project details → confirm order
"""

import os
import re

from agent_config import DEFAULT_MESSAGES, load_tenant_agent_config
from local_history import ingest_message as history_ingest

STEP_MENU = "menu"
STEP_SELECT_SERVICE = "select_service"
STEP_BUDGET = "budget"
STEP_DETAILS = "details"
STEP_CONFIRM = "confirm"
STEP_DONE = "done"


def _short_label(name: str) -> str:
    lower = name.lower()
    if "whatsapp" in lower:
        return "WhatsApp Automation"
    if "pdf" in lower:
        return "PDF Processing"
    if "software" in lower or "web" in lower:
        return "Web Development"
    if "consult" in lower or "support" in lower:
        return "IT Consulting"
    return name.split(" and ")[0][:36]


def _load_services(knowledge_path: str) -> list[dict]:
    services = []
    if not os.path.isfile(knowledge_path):
        return [
            {"id": 1, "name": "WhatsApp business automation and customer support bots", "short": "WhatsApp Automation"},
            {"id": 2, "name": "PDF document processing and data extraction", "short": "PDF Processing"},
            {"id": 3, "name": "Custom software and web development", "short": "Web Development"},
            {"id": 4, "name": "IT consulting and technical support", "short": "IT Consulting"},
        ]
    with open(knowledge_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("- "):
                name = line[2:].strip()
                services.append({"id": len(services) + 1, "name": name, "short": _short_label(name)})
    return services or _load_services("")


def _parse_services_text(text: str) -> list[dict]:
    services = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            name = line[2:].strip()
            services.append({"id": len(services) + 1, "name": name, "short": _short_label(name)})
    return services or _load_services("")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _parse_choice_number(message: str, max_id: int):
    raw = (message or "").strip()
    if raw.isdigit():
        num = int(raw)
        if 1 <= num <= max_id:
            return num
    match = re.search(r"\b([1-9])\b", raw)
    if match:
        num = int(match.group(1))
        if 1 <= num <= max_id:
            return num
    return None


class ConversationFlow:
    def __init__(self, base_dir: str, log_fn, tenant_id: str = "default"):
        self.log = log_fn
        self.tenant_id = tenant_id
        self.base_dir = base_dir
        self.agent_config = load_tenant_agent_config(tenant_id)
        self.services = self._load_services_list()
        self.states = self._load_states()

    def reload_config(self):
        self.agent_config = load_tenant_agent_config(self.tenant_id)

    def reload_states(self):
        from firebase_store import is_active
        if is_active():
            self.states = self._load_states()

    def _msg(self, key: str, **kwargs) -> str:
        template = self.agent_config.get("messages", {}).get(key) or DEFAULT_MESSAGES.get(key, "")
        kwargs.setdefault("business_name", self.agent_config.get("business_name") or "us")
        try:
            return template.format(**kwargs)
        except KeyError:
            return template

    def _load_services_list(self) -> list[dict]:
        from firebase_store import is_active, load_tenant_services_text

        if is_active():
            remote = load_tenant_services_text(self.tenant_id)
            if remote:
                return _parse_services_text(remote)

        knowledge_path = os.path.join(self.base_dir, "knowledge", "services.txt")
        services = _load_services(knowledge_path)
        if services and is_active():
            try:
                from firebase_store import save_tenant_services_text

                with open(knowledge_path, "r", encoding="utf-8") as handle:
                    save_tenant_services_text(self.tenant_id, handle.read())
            except Exception as err:
                self.log("FLOW_WARN", f"Could not sync services to Firebase: {err}")
        return services

    def _load_states(self) -> dict:
        from firebase_store import is_active, load_conversation_states

        if is_active():
            return load_conversation_states(self.tenant_id)
        return {}

    def _save_states(self):
        from firebase_store import is_active, save_conversation_states

        if is_active():
            save_conversation_states(self.tenant_id, self.states)

    def _get(self, chat_name: str) -> dict:
        if chat_name not in self.states:
            self.states[chat_name] = {"step": STEP_SELECT_SERVICE}
        return self.states[chat_name]

    def _service_lines(self) -> str:
        return "\n".join(f"*{s['id']}.* {s['short']}" for s in self.services)

    def _menu_text(self) -> str:
        intro = self._msg("welcome_intro")
        footer = self._msg("welcome_footer", max=len(self.services))
        return f"{intro}\n\n{self._service_lines()}\n\n{footer}"

    def _invalid_selection_text(self) -> str:
        return f"{self._msg('invalid_selection')}\n\n{self._service_lines()}"

    def _service_details_from_rag(self, chat_name: str, service: dict) -> str:
        return self._msg("service_detail_fallback", service=service["short"])

    def _match_service(self, chat_name: str, message: str):
        msg = _normalize(message)
        if msg in ("menu", "start", "restart", "services"):
            return None, True

        choice = _parse_choice_number(message, len(self.services))
        if choice is not None:
            return self.services[choice - 1], False

        for svc in self.services:
            if _normalize(svc["short"]) in msg or msg in _normalize(svc["name"]):
                return svc, False

        return None, False

    def handle_message(self, chat_name: str, user_message: str, extra_context: str = "") -> str:
        self.reload_config()
        self.reload_states()
        msg = (user_message or "").strip()
        state = self._get(chat_name)
        step = state.get("step", STEP_SELECT_SERVICE)

        if _normalize(msg) in ("menu", "restart", "start over", "new order"):
            state["step"] = STEP_SELECT_SERVICE
            self._save_states()
            return self._menu_text()

        if extra_context.strip() and not msg:
            msg = "document received"

        self.log("FLOW", f"'{chat_name}' step={step} msg=\"{msg[:50]}\"")

        if step == STEP_MENU:
            state["step"] = STEP_SELECT_SERVICE
            self._save_states()
            return self._menu_text()

        if step == STEP_SELECT_SERVICE:
            if not msg or _normalize(msg) in ("hi", "hello", "hlo", "hey", "salam", "aoa"):
                return self._menu_text()

            service, want_menu = self._match_service(chat_name, msg)
            if want_menu:
                return self._menu_text()
            if not service:
                return self._invalid_selection_text()

            state["service_id"] = service["id"]
            state["service_name"] = service["name"]
            state["service_short"] = service["short"]
            state["step"] = STEP_BUDGET
            self._save_states()
            detail = self._service_details_from_rag(chat_name, service)
            return self._msg(
                "service_selected",
                service=service["short"],
                detail=detail,
                budget_prompt=self._msg("budget_prompt"),
            )

        if step == STEP_BUDGET:
            if len(msg) < 2:
                return self._msg("budget_too_short")
            state["budget"] = msg
            state["step"] = STEP_DETAILS
            self._save_states()
            return self._msg(
                "budget_recorded",
                budget=msg,
                details_prompt=self._msg("details_prompt"),
            )

        if step == STEP_DETAILS:
            if len(msg) < 3:
                return self._msg("details_too_short")
            state["details"] = msg
            state["step"] = STEP_CONFIRM
            self._save_states()
            return self._msg(
                "order_summary",
                service=state.get("service_short"),
                budget=state.get("budget"),
                details=msg,
            )

        if step == STEP_CONFIRM:
            answer = _normalize(msg)
            if answer in ("yes", "y", "confirm", "ok", "okay", "ha", "han", "jee", "done"):
                state["step"] = STEP_DONE
                self._save_states()
                order_note = (
                    f"ORDER CONFIRMED | {state.get('service_short')} | "
                    f"budget: {state.get('budget')}"
                )
                history_ingest(self.tenant_id, chat_name, order_note, role="client")
                return self._msg(
                    "order_confirmed",
                    service=state.get("service_short"),
                    budget=state.get("budget"),
                    closing=self._msg("order_closing"),
                )
            if answer in ("no", "n", "cancel", "nahi"):
                state["step"] = STEP_SELECT_SERVICE
                self._save_states()
                return self._msg("order_cancelled") + self._menu_text()
            return self._msg("confirm_reminder")

        if step == STEP_DONE:
            if _normalize(msg) in ("menu", "order", "buy", "new"):
                state["step"] = STEP_SELECT_SERVICE
                self._save_states()
                return self._menu_text()
            return self._msg("order_done")

        state["step"] = STEP_SELECT_SERVICE
        self._save_states()
        return self._menu_text()
