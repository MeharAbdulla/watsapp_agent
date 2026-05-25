import asyncio
import json
import os
import threading
import time
from typing import Callable

from tenant_paths import ensure_tenant_layout

# Lazy import bot inside thread to speed API startup


class AgentSession:
  def __init__(self, tenant_id: str, log_fn: Callable):
    self.tenant_id = tenant_id
    self.log_fn = log_fn
    self.status = "idle"
    self.bot = None
    self.thread = None
    self._stop = threading.Event()
    self._ws_queues: list[asyncio.Queue] = []
    self.last_error = None
    ensure_tenant_layout(tenant_id)

  def emit(self, event: str, data: dict | None = None):
    payload = {"event": event, "tenant_id": self.tenant_id, "data": data or {}, "ts": time.time()}
    self.log_fn("WS", json.dumps(payload)[:200])
    for queue in list(self._ws_queues):
      try:
        queue.put_nowait(payload)
      except Exception:
        pass

  def register_ws(self) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    self._ws_queues.append(queue)
    queue.put_nowait({"event": "status", "tenant_id": self.tenant_id, "data": {"status": self.status}})
    return queue

  def unregister_ws(self, queue: asyncio.Queue):
    if queue in self._ws_queues:
      self._ws_queues.remove(queue)

  def _on_bot_event(self, event: str, data: dict):
    if event == "log":
      self.emit("log", data)
    elif event == "status":
      self.status = data.get("status", self.status)
      self.emit("status", data)
    elif event == "qr":
      self.emit("qr", data)

  def _qr_poll_loop(self):
    from bot import WhatsAppBusinessBot

    while not self._stop.is_set() and self.bot and self.bot.driver:
      try:
        if self.bot.is_logged_in():
          self.status = "connected"
          self._on_bot_event("status", {"status": "connected"})
          return
        qr = self.bot.get_qr_base64()
        if qr:
          self.status = "qr_pending"
          self._on_bot_event("qr", {"image_base64": qr})
          self._on_bot_event("status", {"status": "qr_pending"})
      except Exception as err:
        self.last_error = str(err)
      time.sleep(2)

  def _run_bot(self):
    from bot import WhatsAppBusinessBot

    try:
      self.status = "starting"
      self.emit("status", {"status": "starting"})
      self.bot = WhatsAppBusinessBot(
        tenant_id=self.tenant_id,
        on_event=self._on_bot_event,
        managed=True,
        headless=os.getenv("WA_HEADLESS", "false").lower() == "true",
      )
      self.bot.initialize_webdriver(wait_for_login=False)
      qr_thread = threading.Thread(target=self._qr_poll_loop, daemon=True)
      qr_thread.start()

      deadline = time.time() + 180
      while not self._stop.is_set() and time.time() < deadline:
        if self.bot.is_logged_in():
          break
        time.sleep(1)

      if self._stop.is_set():
        return

      if not self.bot.is_logged_in():
        self.status = "qr_pending"
        self.emit("status", {"status": "qr_pending", "message": "Scan QR code with WhatsApp on your phone"})
        while not self._stop.is_set() and not self.bot.is_logged_in():
          time.sleep(2)
          qr = self.bot.get_qr_base64()
          if qr:
            self.emit("qr", {"image_base64": qr})

      if self._stop.is_set():
        return

      self.status = "running"
      self.emit("status", {"status": "running"})
      self.bot.running = True
      self.bot.start_monitoring_loop()
    except Exception as err:
      self.last_error = str(err)
      self.status = "error"
      self.emit("status", {"status": "error", "message": str(err)})
      self.emit("log", {"level": "ERROR", "message": str(err)})
    finally:
      self.status = "stopped"
      self.emit("status", {"status": "stopped"})
      if self.bot:
        try:
          self.bot.shutdown()
        except Exception:
          pass
      self.bot = None

  def start(self):
    if self.thread and self.thread.is_alive():
      return False
    self._stop.clear()
    self.thread = threading.Thread(target=self._run_bot, daemon=True)
    self.thread.start()
    return True

  def stop(self):
    self._stop.set()
    if self.bot:
      self.bot.running = False
      try:
        self.bot.shutdown()
      except Exception:
        pass
    self.status = "stopped"
    self.emit("status", {"status": "stopped"})


class SessionManager:
  def __init__(self):
    self.sessions: dict[str, AgentSession] = {}

  def get(self, tenant_id: str, log_fn) -> AgentSession:
    if tenant_id not in self.sessions:
      self.sessions[tenant_id] = AgentSession(tenant_id, log_fn)
    return self.sessions[tenant_id]


manager = SessionManager()
