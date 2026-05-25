"""
FastAPI SaaS control plane for WhatsApp AI agents.
Run: py -m uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

sys.path.insert(0, BASE_DIR)

from agent_config import DEFAULT_MESSAGES, load_tenant_agent_config, merge_agent_config
from api.database import (
    _using_firebase,
    create_tenant,
    delete_tenant,
    get_tenant_by_api_key,
    get_tenant_by_email,
    init_db,
    list_all_tenants,
)
from api.session_manager import manager
from firebase_store import ensure_firebase, firebase_required, load_tenant_services_text
from local_history import load_chat_history

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "static")

app = FastAPI(
    title="WhatsApp AI Agent SaaS",
    description="Multi-tenant WhatsApp automation with RAG + FAISS",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _log(level, msg):
    print(f"[API] [{level}] {msg}")


def _firebase_setup_message() -> str:
    cred_path = os.path.join(BASE_DIR, "firebase-service-account.json")
    return (
        "Firebase key file missing. In Firebase Console: Project settings > Service accounts > "
        f"Generate new private key. Save the JSON as: {cred_path} then restart the server."
    )


@app.on_event("startup")
def startup():
    os.makedirs(WEB_DIR, exist_ok=True)
    from firebase_store import use_firebase

    if firebase_required() and use_firebase():
        ensure_firebase()
        _log("STARTUP", "Storage: Firebase Firestore (tenants, flow, RAG chunks, PDF text)")
    elif firebase_required():
        _log(
            "STARTUP",
            "WARNING: Add FIREBASE_CREDENTIALS to .env — dashboard works; save/register need Firebase",
        )
    else:
        _log("STARTUP", "USE_FIREBASE=false — limited local mode")
    try:
        init_db()
    except RuntimeError as err:
        _log("STARTUP", f"DB init: {err}")





class LoginBody(BaseModel):
    email: EmailStr
    password: str


class AgentConfigBody(BaseModel):
    bot_mode: str = "gemini"
    business_name: str = ""
    typing_profile: str = "slow"
    services_text: str = ""
    extra_knowledge: str = ""
    messages: dict[str, str] | None = None


class CreateUserBody(BaseModel):
    name: str
    email: EmailStr
    password: str
    is_admin: bool = False


def get_tenant(x_api_key: str = Header(..., alias="X-API-Key")):
    tenant = get_tenant_by_api_key(x_api_key)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return tenant


def get_admin(tenant=Depends(get_tenant)):
    if not tenant.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return tenant


def get_user(tenant=Depends(get_tenant)):
    """Bot/agent endpoints are for normal users only. Admins manage accounts, not bots."""
    if tenant.get("is_admin"):
        raise HTTPException(
            status_code=403,
            detail="Admin accounts cannot use the WhatsApp bot. Sign in as a normal user.",
        )
    return tenant


@app.get("/")
def root():
    index = os.path.join(WEB_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return {"message": "WhatsApp Agent SaaS API", "docs": "/docs"}





@app.post("/api/login")
def login(body: LoginBody):
    if not _using_firebase():
        raise HTTPException(status_code=503, detail=_firebase_setup_message())
    tenant = get_tenant_by_email(body.email)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    
    from firebase_store import hash_password
    if tenant.get("password_hash") != hash_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return {
        "tenant_id": tenant["id"],
        "name": tenant["name"],
        "email": tenant["email"],
        "api_key": tenant["api_key"],
        "is_admin": bool(tenant.get("is_admin", False)),
    }


@app.get("/api/me")
def me(tenant=Depends(get_tenant)):
    is_admin = bool(tenant.get("is_admin", False))
    if is_admin:
        return {
            "tenant_id": tenant["id"],
            "name": tenant["name"],
            "email": tenant["email"],
            "is_admin": True,
        }
    session = manager.get(tenant["id"], _log)
    return {
        "tenant_id": tenant["id"],
        "name": tenant["name"],
        "email": tenant["email"],
        "agent_status": session.status,
        "last_error": session.last_error,
        "is_admin": False,
    }


# --- Admin endpoints ---


@app.get("/api/admin/users")
def admin_list_users(_admin=Depends(get_admin)):
    try:
        return list_all_tenants()
    except RuntimeError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err


@app.post("/api/admin/users")
def admin_create_user(body: CreateUserBody, _admin=Depends(get_admin)):
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    try:
        tenant = create_tenant(body.name.strip(), body.email, body.password, is_admin=body.is_admin)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except RuntimeError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err
    return {
        "id": tenant["id"],
        "name": tenant["name"],
        "email": tenant["email"],
        "is_admin": bool(tenant.get("is_admin", False)),
        "created_at": tenant.get("created_at"),
    }


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, admin=Depends(get_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account while signed in.")
    try:
        deleted = delete_tenant(user_id)
    except RuntimeError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"ok": True, "message": "User deleted"}





@app.get("/api/agent/config")
def get_agent_config(tenant=Depends(get_user)):
    from tenant_paths import ensure_tenant_layout

    cfg = load_tenant_agent_config(tenant["id"])
    services = None
    if _using_firebase():
        try:
            services = load_tenant_services_text(tenant["id"])
        except Exception:
            pass
    if not services:
        paths = ensure_tenant_layout(tenant["id"])
        sp = os.path.join(paths["knowledge"], "services.txt")
        if os.path.isfile(sp):
            with open(sp, "r", encoding="utf-8") as handle:
                services = handle.read()
    if not services:
        global_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge", "services.txt")
        if os.path.isfile(global_path):
            with open(global_path, "r", encoding="utf-8") as handle:
                services = handle.read()
    cfg["services_text"] = services or ""
    cfg["message_keys"] = list(DEFAULT_MESSAGES.keys())
    return cfg


@app.put("/api/agent/config")
def update_agent_config(body: AgentConfigBody, tenant=Depends(get_user)):
    current = load_tenant_agent_config(tenant["id"])
    messages = dict(current.get("messages") or {})
    if body.messages:
        messages.update({k: v for k, v in body.messages.items() if v and str(v).strip()})

    payload = {
        "bot_mode": body.bot_mode if body.bot_mode in ("gemini", "flow") else "gemini",
        "typing_profile": body.typing_profile if body.typing_profile in ("slow", "normal") else "slow",
        "business_name": body.business_name.strip(),
        "messages": messages,
        "extra_knowledge": body.extra_knowledge,
        "services_text": body.services_text,
    }
    try:
        from firebase_store import is_active, save_agent_config as fb_save_agent_config

        if is_active():
            fb_save_agent_config(tenant["id"], payload)
        else:
            from agent_config import save_tenant_agent_config

            save_tenant_agent_config(tenant["id"], payload)
    except RuntimeError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err

    session = manager.get(tenant["id"], _log)
    if session.bot:
        try:
            session.bot.reload_agent_config()
        except Exception:
            pass

    return {
        "ok": True,
        "message": "Agent settings saved",
        "config": merge_agent_config(payload),
    }


@app.get("/api/agent/status")
def agent_status(tenant=Depends(get_user)):
    session = manager.get(tenant["id"], _log)
    return {"status": session.status, "last_error": session.last_error}


@app.post("/api/agent/start")
def agent_start(tenant=Depends(get_user)):
    session = manager.get(tenant["id"], _log)
    if session.thread and session.thread.is_alive():
        return {"ok": True, "status": session.status, "message": "Agent already running"}
    session.start()
    return {"ok": True, "status": session.status, "message": "Agent starting — scan QR on dashboard"}


@app.post("/api/agent/stop")
def agent_stop(tenant=Depends(get_user)):
    session = manager.get(tenant["id"], _log)
    session.stop()
    return {"ok": True, "status": "stopped"}


@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket, api_key: str = ""):
    await websocket.accept()
    if not api_key:
        await websocket.send_json({"event": "error", "data": {"message": "Missing api_key query param"}})
        await websocket.close()
        return

    tenant = get_tenant_by_api_key(api_key)
    if not tenant:
        await websocket.send_json({"event": "error", "data": {"message": "Invalid API key"}})
        await websocket.close()
        return
    if tenant.get("is_admin"):
        await websocket.send_json({
            "event": "error",
            "data": {"message": "Admin accounts cannot use the WhatsApp bot stream."},
        })
        await websocket.close()
        return

    session = manager.get(tenant["id"], _log)
    queue = session.register_ws()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                await websocket.send_json({"event": "ping", "data": {}})
    except WebSocketDisconnect:
        pass
    finally:
        session.unregister_ws(queue)


# --- CRM Endpoints ---

class UpdateLeadStageBody(BaseModel):
    stage: str

class UpdateLeadNotesBody(BaseModel):
    notes: str

class UpdateLeadStepBody(BaseModel):
    step: str

class SendManualMessageBody(BaseModel):
    message: str


@app.get("/api/crm/leads")
def get_crm_leads(tenant=Depends(get_user)):
    from firebase_store import load_conversation_states, is_active
    if is_active():
        states = load_conversation_states(tenant["id"])
    else:
        session = manager.get(tenant["id"], _log)
        if session.bot and hasattr(session.bot, "flow"):
            states = session.bot.flow.states
        else:
            states = {}

    leads = []
    for chat_name, info in states.items():
        lead = {
            "name": chat_name,
            "step": info.get("step", "menu"),
            "service_short": info.get("service_short", ""),
            "budget": info.get("budget", ""),
            "details": info.get("details", ""),
            "paused": info.get("paused", False),
            "notes": info.get("notes", ""),
            "stage": info.get("stage", "New"),
            "last_active": info.get("last_active", datetime.utcnow().isoformat()),
        }
        leads.append(lead)

    leads.sort(key=lambda x: x.get("last_active", ""), reverse=True)
    return leads


@app.put("/api/crm/leads/{chat_name}/stage")
def update_lead_stage(chat_name: str, body: UpdateLeadStageBody, tenant=Depends(get_user)):
    from firebase_store import load_conversation_states, save_conversation_states, is_active
    if is_active():
        states = load_conversation_states(tenant["id"])
    else:
        session = manager.get(tenant["id"], _log)
        states = session.bot.flow.states if (session.bot and hasattr(session.bot, "flow")) else {}
        
    if chat_name not in states:
        states[chat_name] = {"step": "menu"}
    states[chat_name]["stage"] = body.stage
    states[chat_name]["last_active"] = datetime.utcnow().isoformat()
    
    if is_active():
        save_conversation_states(tenant["id"], states)
    return {"ok": True, "message": f"Stage updated to {body.stage}"}


@app.put("/api/crm/leads/{chat_name}/notes")
def update_lead_notes(chat_name: str, body: UpdateLeadNotesBody, tenant=Depends(get_user)):
    from firebase_store import load_conversation_states, save_conversation_states, is_active
    if is_active():
        states = load_conversation_states(tenant["id"])
    else:
        session = manager.get(tenant["id"], _log)
        states = session.bot.flow.states if (session.bot and hasattr(session.bot, "flow")) else {}
        
    if chat_name not in states:
        states[chat_name] = {"step": "menu"}
    states[chat_name]["notes"] = body.notes
    states[chat_name]["last_active"] = datetime.utcnow().isoformat()
    
    if is_active():
        save_conversation_states(tenant["id"], states)
    return {"ok": True, "message": "Notes updated"}


@app.put("/api/crm/leads/{chat_name}/step")
def update_lead_step(chat_name: str, body: UpdateLeadStepBody, tenant=Depends(get_user)):
    from firebase_store import load_conversation_states, save_conversation_states, is_active
    if is_active():
        states = load_conversation_states(tenant["id"])
    else:
        session = manager.get(tenant["id"], _log)
        states = session.bot.flow.states if (session.bot and hasattr(session.bot, "flow")) else {}
        
    if chat_name not in states:
        states[chat_name] = {}
    states[chat_name]["step"] = body.step
    states[chat_name]["last_active"] = datetime.utcnow().isoformat()
    
    if is_active():
        save_conversation_states(tenant["id"], states)
    return {"ok": True, "message": f"Bot step updated to {body.step}"}


@app.put("/api/crm/leads/{chat_name}/pause")
def update_lead_pause(chat_name: str, paused: bool, tenant=Depends(get_user)):
    from firebase_store import load_conversation_states, save_conversation_states, is_active
    if is_active():
        states = load_conversation_states(tenant["id"])
    else:
        session = manager.get(tenant["id"], _log)
        states = session.bot.flow.states if (session.bot and hasattr(session.bot, "flow")) else {}
        
    if chat_name not in states:
        states[chat_name] = {"step": "menu"}
    states[chat_name]["paused"] = paused
    states[chat_name]["last_active"] = datetime.utcnow().isoformat()
    
    if is_active():
        save_conversation_states(tenant["id"], states)
    return {"ok": True, "paused": paused, "message": "Bot status updated"}


@app.get("/api/crm/leads/{chat_name}/history")
def get_lead_history(chat_name: str, tenant=Depends(get_user)):
    chunks = load_chat_history(tenant["id"], chat_name)
    messages = []
    for chunk in chunks:
        if isinstance(chunk, dict) and "text" in chunk:
            messages.append({
                "role": chunk.get("role", "unknown"),
                "text": chunk.get("text", ""),
                "indexed_at": chunk.get("indexed_at", ""),
                "source": chunk.get("source", "")
            })
    return messages


@app.post("/api/crm/leads/{chat_name}/message")
def send_manual_message(chat_name: str, body: SendManualMessageBody, tenant=Depends(get_user)):
    session = manager.get(tenant["id"], _log)
    if not session.bot or not session.bot.driver:
        raise HTTPException(status_code=503, detail="WhatsApp Agent is not running. Start the agent first.")
    
    from firebase_store import load_conversation_states, save_conversation_states, is_active
    if is_active():
        states = load_conversation_states(tenant["id"])
    else:
        states = session.bot.flow.states if hasattr(session.bot, "flow") else {}
        
    if chat_name not in states:
        states[chat_name] = {"step": "menu"}
    states[chat_name]["paused"] = True
    states[chat_name]["last_active"] = datetime.utcnow().isoformat()
    
    if is_active():
        save_conversation_states(tenant["id"], states)
    
    bot = session.bot
    success = bot.open_chat_by_name(chat_name)
    if not success:
        raise HTTPException(status_code=500, detail=f"Could not locate or open chat for '{chat_name}'")
        
    send_success = bot.send_reply(body.message)
    if not send_success:
        raise HTTPException(status_code=500, detail="Failed to send message")
        
    bot.remember_exchange(chat_name, "", f"[assistant]: {body.message}")
    return {"ok": True, "message": "Manual message sent successfully, bot is now paused for this lead."}
def runtime_error_handler(_request, exc: RuntimeError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
