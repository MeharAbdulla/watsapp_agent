/* =============================================================
   AgentCloud dashboard — UI logic
   ============================================================= */

const API = window.location.origin;
let apiKey = localStorage.getItem("api_key") || "";
let isAdminUser = localStorage.getItem("is_admin") === "1";
let ws = null;
let tab = "login";
let currentView = "overview";
let currentAdminView = "users";

const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);

function headers() {
  return { "Content-Type": "application/json", "X-API-Key": apiKey };
}

/** Safely parse server response. Avoids "Internal Server Error" JSON crashes. */
async function parseApiResponse(res) {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    const snippet = text.slice(0, 140).replace(/\s+/g, " ");
    throw new Error(res.ok ? "Invalid server response" : snippet || `Request failed (${res.status})`);
  }
}

/* ----------------- TOASTS ----------------- */

function toast(message, opts = {}) {
  const kind = opts.kind || "info";
  const title = opts.title || ({ success: "Success", error: "Error", warn: "Heads up" }[kind] || "Info");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<div><div class="toast-title">${escapeHTML(title)}</div><div class="toast-msg">${escapeHTML(message)}</div></div>`;
  $("toast-stack").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateX(20px)";
    el.style.transition = "all 0.25s";
    setTimeout(() => el.remove(), 250);
  }, opts.duration || 4200);
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

/* ----------------- BUTTON LOADING STATES ----------------- */

function setLoading(btn, on) {
  if (!btn) return;
  btn.disabled = !!on;
  const label = btn.querySelector(".btn-label");
  const spinner = btn.querySelector(".spinner");
  if (label && spinner) {
    label.style.opacity = on ? "0.4" : "";
    spinner.classList.toggle("hidden", !on);
  }
}

/* ----------------- VIEW SWITCHING ----------------- */

const VIEW_TITLES = {
  overview:  { title: "Overview",       sub: "Live status of your WhatsApp agent" },
  crm:       { title: "CRM Leads",      sub: "Manage client pipelines, conversation takeover, and AI insights" },
  agent:     { title: "Agent persona",  sub: "Customize tone, speed, and identity" },
  messages:  { title: "Messages",       sub: "Tune every reply your agent sends" },
  knowledge: { title: "Knowledge base", sub: "Feed your agent business facts and FAQs" },
  logs:      { title: "Activity log",   sub: "All events from your agent in real time" },
};

function setView(view) {
  currentView = view;
  $$(".side-link").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  $$(".view").forEach((el) => el.classList.toggle("hidden", el.dataset.view !== view));
  const meta = VIEW_TITLES[view] || VIEW_TITLES.overview;
  $("view-title").textContent = meta.title;
  $("view-sub").textContent = meta.sub;
  if (view === "crm") {
    loadCrmLeads();
  }
}

/* ----------------- AUTH ----------------- */

function showAuth() {
  $("panel-auth").classList.remove("hidden");
  $("panel-dash").classList.add("hidden");
  const adminPanel = $("panel-admin");
  if (adminPanel) adminPanel.classList.add("hidden");
}

function showDash() {
  $("panel-auth").classList.add("hidden");
  const adminPanel = $("panel-admin");
  if (adminPanel) adminPanel.classList.add("hidden");
  $("panel-dash").classList.remove("hidden");
  setView("overview");
  connectWebSocket();
  refreshStatus();
  loadAgentConfig();
}

function showAdmin() {
  $("panel-auth").classList.add("hidden");
  $("panel-dash").classList.add("hidden");
  const adminPanel = $("panel-admin");
  if (!adminPanel) return;
  adminPanel.classList.remove("hidden");
  setAdminView("users");
  refreshAdminMe();
  loadAdminUsers();
}

function updateUser(name, email) {
  $("user-name").textContent = name || "Workspace";
  $("user-mail").textContent = email || "";
  const initial = (name || "A").trim().charAt(0).toUpperCase();
  $("user-avatar").textContent = initial;
}

/* ----------------- STATUS / WEBSOCKET ----------------- */

function setStatus(status) {
  status = (status || "idle").trim();
  const chip = $("status-chip");
  chip.dataset.status = status.replace(/\s/g, "_");
  chip.querySelector(".status-text").textContent = status;
  const display = status.charAt(0).toUpperCase() + status.slice(1);
  $("stat-status").textContent = display;

  if (status === "connected" || status === "running") {
    $("stat-status-hint").textContent = "Agent is online and replying to messages.";
  } else if (status === "qr_pending") {
    $("stat-status-hint").textContent = "Scan the QR code with WhatsApp to continue.";
  } else if (status === "starting") {
    $("stat-status-hint").textContent = "Launching Chrome…";
  } else if (status === "stopped") {
    $("stat-status-hint").textContent = "Agent has been stopped.";
  } else if (status === "error") {
    $("stat-status-hint").textContent = "Something went wrong. Check the activity log.";
  } else {
    $("stat-status-hint").textContent = "Press start to launch your agent.";
  }
}

const LEVEL_CLASS = {
  ERROR: "error", CRITICAL: "error",
  SUCCESS: "success",
  WARN: "warn", WARNING: "warn",
};

function addLog(level, message) {
  [$("logs"), $("logs-full")].forEach((el) => {
    if (!el) return;
    const empty = el.querySelector(".log-empty");
    if (empty) empty.remove();
    const row = document.createElement("div");
    row.className = "log-row " + (LEVEL_CLASS[level] || "info");
    row.innerHTML = `
      <span class="log-time">${new Date().toLocaleTimeString([], { hour12: false })}</span>
      <span class="log-level">${escapeHTML(level || "log")}</span>
      <span class="log-msg">${escapeHTML(message || "")}</span>`;
    el.appendChild(row);
    el.scrollTop = el.scrollHeight;
    const rows = el.querySelectorAll(".log-row");
    if (rows.length > 500) rows[0].remove();
  });
}

function clearLogs() {
  [$("logs"), $("logs-full")].forEach((el) => {
    if (!el) return;
    el.innerHTML = '<div class="log-empty">Logs will appear here once the agent starts.</div>';
  });
}

function connectWebSocket() {
  if (ws) try { ws.close(); } catch {}
  if (!apiKey) return;
  const url = `${API.replace("http", "ws")}/ws/agent?api_key=${encodeURIComponent(apiKey)}`;
  ws = new WebSocket(url);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.event === "ping") return;
      if (msg.event === "error") { addLog("ERROR", msg.data?.message || "WebSocket error"); return; }
      if (msg.event === "log") addLog(msg.data.level, msg.data.message);
      if (msg.event === "status") {
        setStatus(msg.data.status);
        if (msg.data.message) $("agent-hint").textContent = msg.data.message;
      }
      if (msg.event === "qr" && msg.data.image_base64) {
        $("qr-placeholder").classList.add("hidden");
        const img = $("qr-image");
        img.src = "data:image/png;base64," + msg.data.image_base64;
        img.classList.remove("hidden");
      }
    } catch {
      addLog("WARN", "Bad WebSocket message");
    }
  };
  ws.onclose = () => { if (apiKey) setTimeout(connectWebSocket, 3000); };
}



/* ----------------- MESSAGE FIELDS ----------------- */

const MESSAGE_LABELS = {
  welcome_intro: "Welcome message",
  welcome_footer: "Menu footer",
  invalid_selection: "Invalid option",
  service_selected: "After service selected",
  budget_prompt: "Ask for budget",
  budget_too_short: "Budget too short",
  budget_recorded: "Budget saved",
  details_prompt: "Ask for details",
  details_too_short: "Details too short",
  order_summary: "Order summary",
  confirm_reminder: "Confirm reminder",
  order_confirmed: "Order confirmed",
  order_closing: "Closing line",
  order_cancelled: "Order cancelled",
  order_done: "Already confirmed",
  service_detail_fallback: "Default service info",
};

function renderMessageFields(messages, keys) {
  const wrap = $("cfg-messages");
  if (!wrap) return;
  wrap.innerHTML = "";
  const list = keys || Object.keys(MESSAGE_LABELS);
  list.forEach((key) => {
    const label = MESSAGE_LABELS[key] || key;
    const value = (messages && messages[key]) || "";
    const card = document.createElement("div");
    card.className = "msg-card";
    card.innerHTML = `
      <div class="msg-label">${escapeHTML(label)}<span class="msg-key">${escapeHTML(key)}</span></div>
      <textarea data-msg-key="${key}" rows="3" placeholder="Type your message…"></textarea>`;
    card.querySelector("textarea").value = value;
    wrap.appendChild(card);
  });
}

/* ----------------- AGENT CONFIG ----------------- */

async function loadAgentConfig() {
  if (!apiKey) return;
  try {
    const res = await fetch(`${API}/api/agent/config`, { headers: headers() });
    const data = await parseApiResponse(res);
    if (!res.ok) { toast(data.detail || "Couldn't load settings", { kind: "warn" }); return; }
    $("cfg-business").value = data.business_name || "";
    $("cfg-bot-mode").value = data.bot_mode || "gemini";
    $("cfg-typing").value = data.typing_profile || "slow";
    $("cfg-services").value = data.services_text || "";
    $("cfg-knowledge").value = data.extra_knowledge || "";
    renderMessageFields(data.messages, data.message_keys);
    $("stat-typing").textContent = data.typing_profile === "normal" ? "Normal" : "Slow & human-like";
  } catch (err) {
    toast("Couldn't load settings: " + err.message, { kind: "warn" });
  }
}

async function saveAgentConfig(triggerBtn) {
  setLoading(triggerBtn, true);
  const messages = {};
  $$("#cfg-messages textarea[data-msg-key]").forEach((el) => {
    if (el.value.trim()) messages[el.dataset.msgKey] = el.value.trim();
  });
  const body = {
    bot_mode: $("cfg-bot-mode").value,
    business_name: $("cfg-business").value.trim(),
    typing_profile: $("cfg-typing").value,
    services_text: $("cfg-services").value,
    extra_knowledge: $("cfg-knowledge").value,
    messages,
  };
  try {
    const res = await fetch(`${API}/api/agent/config`, {
      method: "PUT",
      headers: headers(),
      body: JSON.stringify(body),
    });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Save failed");
    toast("Settings saved", { kind: "success" });
    $("stat-typing").textContent = body.typing_profile === "normal" ? "Normal" : "Slow & human-like";
  } catch (err) {
    toast(err.message, { kind: "error" });
  } finally {
    setLoading(triggerBtn, false);
  }
}

async function refreshStatus() {
  if (!apiKey) return;
  try {
    const res = await fetch(`${API}/api/me`, { headers: headers() });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Session expired");
    if (data.is_admin) {
      isAdminUser = true;
      localStorage.setItem("is_admin", "1");
      updateAdminUser(data.name, data.email);
      showAdmin();
      return;
    }
    setStatus(data.agent_status);
    updateUser(data.name, data.email);
  } catch (err) {
    addLog("WARN", err.message);
    localStorage.removeItem("api_key");
    apiKey = "";
    showAuth();
  }
}

/* ----------------- EVENT WIRING ----------------- */



$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("auth-error").classList.add("hidden");
  setLoading($("auth-submit"), true);
  const email = $("input-email").value.trim();
  const password = $("input-password").value;
  try {
    const res = await fetch(`${API}/api/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Request failed");
    apiKey = data.api_key;
    isAdminUser = !!data.is_admin;
    localStorage.setItem("api_key", apiKey);
    localStorage.setItem("is_admin", isAdminUser ? "1" : "0");
    if (isAdminUser) {
      updateAdminUser(data.name, data.email);
      showAdmin();
    } else {
      updateUser(data.name, data.email);
      showDash();
    }
  } catch (err) {
    $("auth-error").textContent = err.message;
    $("auth-error").classList.remove("hidden");
  } finally {
    setLoading($("auth-submit"), false);
  }
});

$("btn-copy-key").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(apiKey);
    toast("API key copied to clipboard", { kind: "success" });
  } catch {
    toast("Couldn't copy — copy it manually", { kind: "warn" });
  }
});

$("btn-enter-dash").addEventListener("click", () => {
  localStorage.setItem("api_key", apiKey);
  const name = $("input-name").value.trim() || "Workspace";
  const email = $("input-email").value.trim();
  updateUser(name, email);
  showDash();
});

$$(".side-link").forEach((btn) => {
  btn.addEventListener("click", () => setView(btn.dataset.view));
});

function signOut() {
  localStorage.removeItem("api_key");
  localStorage.removeItem("is_admin");
  apiKey = "";
  isAdminUser = false;
  if (ws) try { ws.close(); } catch {}
  showAuth();
  toast("Signed out", { kind: "info" });
}

$("btn-logout").addEventListener("click", signOut);

const btnAdminLogout = $("btn-admin-logout");
if (btnAdminLogout) btnAdminLogout.addEventListener("click", signOut);

$("btn-start").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  setLoading(btn, true);
  $("agent-hint").textContent = "Starting Chrome…";
  try {
    const res = await fetch(`${API}/api/agent/start`, { method: "POST", headers: headers() });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Start failed");
    $("agent-hint").textContent = data.message || "";
    setStatus(data.status);
    toast(data.message || "Agent starting", { kind: "success" });
  } catch (err) {
    $("agent-hint").textContent = err.message;
    toast(err.message, { kind: "error" });
  } finally {
    setLoading(btn, false);
  }
});

$("btn-stop").addEventListener("click", async () => {
  try {
    await fetch(`${API}/api/agent/stop`, { method: "POST", headers: headers() });
  } catch { /* ignore */ }
  $("qr-image").classList.add("hidden");
  $("qr-placeholder").classList.remove("hidden");
  setStatus("stopped");
  toast("Agent stopped", { kind: "info" });
});

["btn-save-config-1", "btn-save-config-2", "btn-save-config-3"].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener("click", (e) => saveAgentConfig(e.currentTarget));
});

["btn-clear-logs", "btn-clear-logs-2"].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener("click", clearLogs);
});

/* ===========================================================
   CRM CLIENT PIPELINE LOGIC
   =========================================================== */

let currentSelectedLead = null;
let leadsList = [];
let activeCrmFilter = "all";
let activeCrmSearch = "";

async function loadCrmLeads() {
  const container = $("crm-leads-list-container");
  if (!container) return;
  try {
    const res = await fetch(`${API}/api/crm/leads`, { headers: headers() });
    if (!res.ok) throw new Error("Could not fetch CRM leads");
    leadsList = await res.json();
    renderLeadsList();
  } catch (err) {
    container.innerHTML = `<div class="crm-no-leads">Error loading pipeline: ${err.message}</div>`;
  }
}

function renderLeadsList() {
  const container = $("crm-leads-list-container");
  if (!container) return;
  
  const query = activeCrmSearch.toLowerCase().trim();
  const filtered = leadsList.filter(lead => {
    const matchesSearch = !query || lead.name.toLowerCase().includes(query) || (lead.budget && String(lead.budget).toLowerCase().includes(query)) || (lead.service_short && lead.service_short.toLowerCase().includes(query));
    const matchesFilter = activeCrmFilter === "all" || lead.stage.toLowerCase() === activeCrmFilter.toLowerCase();
    return matchesSearch && matchesFilter;
  });

  if (filtered.length === 0) {
    container.innerHTML = `<div class="crm-no-leads">No leads match current filters</div>`;
    return;
  }

  container.innerHTML = "";
  filtered.forEach(lead => {
    const card = document.createElement("div");
    card.className = `crm-lead-card ${currentSelectedLead === lead.name ? "active" : ""}`;
    card.dataset.name = lead.name;

    // Relative last active time format
    let activeTimeStr = "Recent";
    try {
      const dt = new Date(lead.last_active);
      if (!isNaN(dt)) {
        activeTimeStr = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      }
    } catch (e) {}

    const stageClass = lead.stage.toLowerCase().replace(/\s+/g, '-');
    const pausedIndicator = lead.paused ? `<span class="crm-pause-badge">Takeover</span>` : "";

    card.innerHTML = `
      <div class="crm-lead-card-header">
        <span class="crm-lead-name" title="${lead.name}">${lead.name}</span>
        <span class="crm-lead-time">${activeTimeStr}</span>
      </div>
      <div class="crm-lead-card-body">
        <span class="crm-lead-step">Step: ${lead.step || 'menu'}</span>
        <span class="crm-badge ${stageClass}">${lead.stage || 'New'}</span>
      </div>
      ${pausedIndicator ? `<div class="crm-lead-card-footer">${pausedIndicator}</div>` : ""}
    `;

    card.addEventListener("click", () => selectLead(lead.name));
    container.appendChild(card);
  });
}

async function selectLead(name) {
  currentSelectedLead = name;
  
  // Update class lists on cards
  $$(".crm-lead-card").forEach(el => el.classList.toggle("active", el.dataset.name === name));
  
  const lead = leadsList.find(l => l.name === name);
  if (!lead) return;

  const panel = $("crm-panel-container");
  if (!panel) return;

  // Let's calculate AI lead score
  let leadScore = 0;
  let scoreText = "Cold Lead";
  if (lead.step === "confirm" || lead.step === "done") {
    leadScore = 100;
    scoreText = "Hot Lead (Ready)";
  } else if (lead.step === "details") {
    leadScore = 75;
    scoreText = "Warm Lead (High Intent)";
  } else if (lead.step === "budget") {
    leadScore = 50;
    scoreText = "Warm Lead (Mid Intent)";
  } else if (lead.step === "service" || lead.step === "menu") {
    leadScore = 20;
    scoreText = "Cold Lead";
  }

  panel.innerHTML = `
    <!-- Header -->
    <div class="crm-header">
      <div class="crm-header-info">
        <h2 id="crm-current-name">${lead.name}</h2>
      </div>
      <div class="crm-header-actions">
        <div class="crm-toggle-container">
          <span>Pause Bot</span>
          <label class="crm-toggle-switch">
            <input type="checkbox" id="crm-bot-pause-toggle" ${lead.paused ? "checked" : ""}>
            <span class="crm-toggle-slider"></span>
          </label>
        </div>

        <select id="crm-stage-select" class="crm-dropdown">
          <option value="New" ${lead.stage === "New" ? "selected" : ""}>Stage: New</option>
          <option value="Contacted" ${lead.stage === "Contacted" ? "selected" : ""}>Stage: Contacted</option>
          <option value="In Progress" ${lead.stage === "In Progress" ? "selected" : ""}>Stage: In Progress</option>
          <option value="Ordered" ${lead.stage === "Ordered" ? "selected" : ""}>Stage: Ordered</option>
          <option value="Closed" ${lead.stage === "Closed" ? "selected" : ""}>Stage: Closed</option>
        </select>

        <select id="crm-step-select" class="crm-dropdown">
          <option value="menu" ${lead.step === "menu" ? "selected" : ""}>Step: Menu</option>
          <option value="service" ${lead.step === "service" ? "selected" : ""}>Step: Service</option>
          <option value="budget" ${lead.step === "budget" ? "selected" : ""}>Step: Budget</option>
          <option value="details" ${lead.step === "details" ? "selected" : ""}>Step: Details</option>
          <option value="confirm" ${lead.step === "confirm" ? "selected" : ""}>Step: Confirm</option>
          <option value="done" ${lead.step === "done" ? "selected" : ""}>Step: Done</option>
        </select>
      </div>
    </div>

    <!-- Body columns -->
    <div class="crm-body-grid">
      <!-- Column 1: Details and profile info -->
      <div class="crm-details-pane">
        <!-- AI Insights & Score -->
        <div>
          <div class="crm-section-title">AI Lead Scoring & Profile</div>
          <div class="crm-insight-card">
            <div class="crm-insight-row">
              <span class="crm-data-label">Lead Rating</span>
              <span class="crm-insight-val">${scoreText}</span>
            </div>
            <div class="crm-progress-bar-bg">
              <div class="crm-progress-bar-fill" style="width: ${leadScore}%"></div>
            </div>
            <div class="crm-ai-summary">
              ${lead.step === "done" 
                ? "This client has completed the purchase flow and order is registered." 
                : lead.step === "confirm" 
                ? "AI Bot is waiting for the client to confirm their service details." 
                : lead.step === "details"
                ? "Client is currently providing required delivery details."
                : "Client is in early conversation stages describing requirements."
              }
            </div>
          </div>
        </div>

        <!-- Order details extracted -->
        <div>
          <div class="crm-section-title">Order Context Extracted</div>
          <div class="crm-data-list">
            <div class="crm-data-item">
              <span class="crm-data-label">Selected Service</span>
              <div class="crm-data-value">${lead.service_short || "Not specified yet"}</div>
            </div>
            <div class="crm-data-item">
              <span class="crm-data-label">Budget Range</span>
              <div class="crm-data-value">${lead.budget || "Not specified yet"}</div>
            </div>
            <div class="crm-data-item">
              <span class="crm-data-label">Project Details</span>
              <div class="crm-data-value">${lead.details || "Not specified yet"}</div>
            </div>
          </div>
        </div>

        <!-- Internal Notes -->
        <div>
          <div class="crm-section-title">Internal CRM Notes</div>
          <textarea id="crm-lead-notes" class="crm-notes-textarea" placeholder="Add private notes about this client (e.g. key details, preference, manual follow-ups)...">${lead.notes || ""}</textarea>
          <button id="crm-save-notes-btn" class="btn btn-ghost btn-sm btn-block" style="margin-top: 0.5rem;">Save Notes</button>
        </div>
      </div>

      <!-- Column 2: Live Chat & Direct text compose -->
      <div class="crm-chat-pane">
        <div class="crm-chat-messages" id="crm-chat-messages-container">
          <div class="crm-chat-empty">Loading transcript...</div>
        </div>

        <div class="crm-chat-compose">
          <div class="crm-compose-warning">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            <span>Sending a manual message will automatically Pause the bot.</span>
          </div>
          <div class="crm-compose-row">
            <input type="text" id="crm-message-input" class="crm-compose-textarea" placeholder="Type a message to take over and reply directly..." />
            <button id="crm-send-msg-btn" class="btn btn-primary btn-sm">Send</button>
          </div>
        </div>
      </div>
    </div>
  `;

  // Attach dynamic event listeners
  $("crm-bot-pause-toggle").addEventListener("change", async (e) => {
    const isPaused = e.target.checked;
    try {
      const res = await fetch(`${API}/api/crm/leads/${encodeURIComponent(name)}/pause?paused=${isPaused}`, {
        method: "PUT",
        headers: headers()
      });
      if (!res.ok) throw new Error("Failed to change pause status");
      toast(isPaused ? "Bot paused for takeover" : "Bot active", { kind: isPaused ? "warn" : "success" });
      loadCrmLeads();
    } catch (err) {
      toast(err.message, { kind: "error" });
      e.target.checked = !isPaused;
    }
  });

  $("crm-stage-select").addEventListener("change", async (e) => {
    const newStage = e.target.value;
    try {
      const res = await fetch(`${API}/api/crm/leads/${encodeURIComponent(name)}/stage`, {
        method: "PUT",
        headers: headers(),
        body: JSON.stringify({ stage: newStage })
      });
      if (!res.ok) throw new Error("Failed to update stage");
      toast(`Pipeline stage updated to ${newStage}`, { kind: "success" });
      loadCrmLeads();
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  });

  $("crm-step-select").addEventListener("change", async (e) => {
    const newStep = e.target.value;
    try {
      const res = await fetch(`${API}/api/crm/leads/${encodeURIComponent(name)}/step`, {
        method: "PUT",
        headers: headers(),
        body: JSON.stringify({ step: newStep })
      });
      if (!res.ok) throw new Error("Failed to update step");
      toast(`Bot step forced to ${newStep}`, { kind: "success" });
      loadCrmLeads();
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  });

  $("crm-save-notes-btn").addEventListener("click", async () => {
    const notesEl = $("crm-lead-notes");
    const notesVal = notesEl.value;
    try {
      const res = await fetch(`${API}/api/crm/leads/${encodeURIComponent(name)}/notes`, {
        method: "PUT",
        headers: headers(),
        body: JSON.stringify({ notes: notesVal })
      });
      if (!res.ok) throw new Error("Failed to save notes");
      toast("Internal notes updated", { kind: "success" });
      loadCrmLeads();
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  });

  $("crm-send-msg-btn").addEventListener("click", () => submitManualMessage(name));
  $("crm-message-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitManualMessage(name);
    }
  });

  // Fetch chat history
  loadLeadHistory(name);
}

async function loadLeadHistory(name) {
  const container = $("crm-chat-messages-container");
  if (!container) return;

  try {
    const res = await fetch(`${API}/api/crm/leads/${encodeURIComponent(name)}/history`, { headers: headers() });
    if (!res.ok) throw new Error("Could not fetch chat history");
    const messages = await res.json();

    if (messages.length === 0) {
      container.innerHTML = `<div class="crm-chat-empty">No conversation history yet.</div>`;
      return;
    }

    container.innerHTML = "";
    messages.forEach(msg => {
      const bubble = document.createElement("div");
      const isClient = msg.role === "client";
      bubble.className = `crm-bubble ${isClient ? "client" : "assistant"}`;
      
      let dateStr = "";
      try {
        const dt = new Date(msg.indexed_at);
        if (!isNaN(dt)) {
          dateStr = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
      } catch(e){}

      bubble.innerHTML = `
        <div>${escapeHtml(msg.text)}</div>
        <div class="crm-bubble-time">${dateStr}</div>
      `;
      container.appendChild(bubble);
    });

    // Scroll to bottom
    container.scrollTop = container.scrollHeight;
  } catch (err) {
    container.innerHTML = `<div class="crm-chat-empty">Error loading messages: ${err.message}</div>`;
  }
}

async function submitManualMessage(name) {
  const input = $("crm-message-input");
  const btn = $("crm-send-msg-btn");
  const msgVal = input.value.trim();
  if (!msgVal) return;

  input.disabled = true;
  btn.disabled = true;
  
  try {
    const res = await fetch(`${API}/api/crm/leads/${encodeURIComponent(name)}/message`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ message: msgVal })
    });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Failed to send message via WhatsApp agent");
    
    input.value = "";
    toast("Message sent, bot paused for takeover", { kind: "success" });
    
    // Auto-update UI toggle to pause status
    const toggle = $("crm-bot-pause-toggle");
    if (toggle) toggle.checked = true;

    // Reload list and chat logs
    await loadCrmLeads();
    await loadLeadHistory(name);
  } catch (err) {
    toast(err.message, { kind: "error" });
  } finally {
    input.disabled = false;
    btn.disabled = false;
    input.focus();
  }
}

function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

// Bind search and filtering events
document.addEventListener("DOMContentLoaded", () => {
  const searchInput = $("crm-lead-search");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      activeCrmSearch = e.target.value;
      renderLeadsList();
    });
  }

  $$(".crm-filter-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      $$(".crm-filter-btn").forEach(b => b.classList.remove("active"));
      e.currentTarget.classList.add("active");
      activeCrmFilter = e.currentTarget.dataset.filter;
      renderLeadsList();
    });
  });
});

/* ===========================================================
   ADMIN DASHBOARD LOGIC
   =========================================================== */

const ADMIN_VIEW_META = {
  users:  { title: "All Users",   sub: "Every workspace and admin in your AgentCloud instance." },
  create: { title: "Create User", sub: "Provision a new workspace or invite another admin." },
};

function setAdminView(view) {
  currentAdminView = view;
  document.querySelectorAll('[data-admin-view]').forEach((el) => {
    if (el.tagName === "BUTTON") {
      el.classList.toggle("active", el.dataset.adminView === view);
    } else {
      el.classList.toggle("hidden", el.dataset.adminView !== view);
    }
  });
  const meta = ADMIN_VIEW_META[view] || ADMIN_VIEW_META.users;
  const title = $("admin-view-title");
  const sub = $("admin-view-sub");
  if (title) title.textContent = meta.title;
  if (sub) sub.textContent = meta.sub;
  if (view === "users") loadAdminUsers();
}

function updateAdminUser(name, email) {
  const nameEl = $("admin-name");
  const mailEl = $("admin-mail");
  const av = $("admin-avatar");
  if (nameEl) nameEl.textContent = name || "Admin";
  if (mailEl) mailEl.textContent = email || "";
  if (av) av.textContent = (name || "A").trim().charAt(0).toUpperCase();
}

async function refreshAdminMe() {
  if (!apiKey) return;
  try {
    const res = await fetch(`${API}/api/me`, { headers: headers() });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Session expired");
    if (!data.is_admin) {
      // Account is no longer admin — bounce back to user dashboard.
      isAdminUser = false;
      localStorage.setItem("is_admin", "0");
      updateUser(data.name, data.email);
      showDash();
      return;
    }
    updateAdminUser(data.name, data.email);
  } catch (err) {
    toast(err.message || "Session expired", { kind: "warn" });
    signOut();
  }
}

async function loadAdminUsers() {
  const wrap = $("admin-users-wrap");
  if (!wrap) return;
  wrap.innerHTML = '<div class="admin-loading">Loading users…</div>';
  try {
    const res = await fetch(`${API}/api/admin/users`, { headers: headers() });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Failed to load users");
    renderAdminUsers(data);
  } catch (err) {
    wrap.innerHTML = `<div class="admin-error">Could not load users: ${escapeHTML(err.message)}</div>`;
  }
}

function renderAdminUsers(users) {
  const wrap = $("admin-users-wrap");
  if (!wrap) return;
  if (!Array.isArray(users) || users.length === 0) {
    wrap.innerHTML = '<div class="admin-empty">No users yet. Click "+ New user" to create the first one.</div>';
    return;
  }

  const rows = users.map((u) => {
    let created = "—";
    try {
      const dt = new Date(u.created_at);
      if (!isNaN(dt)) created = dt.toLocaleString();
    } catch (_) {}
    const roleBadge = u.is_admin
      ? '<span class="role-badge role-admin">Admin</span>'
      : '<span class="role-badge role-user">User</span>';
    return `
      <tr>
        <td>
          <div class="admin-user-cell">
            <div class="admin-user-avatar">${escapeHTML((u.name || "?").trim().charAt(0).toUpperCase())}</div>
            <div>
              <div class="admin-user-name">${escapeHTML(u.name || "(no name)")}</div>
              <div class="admin-user-id">${escapeHTML(u.id || "")}</div>
            </div>
          </div>
        </td>
        <td>${escapeHTML(u.email || "")}</td>
        <td>${roleBadge}</td>
        <td>${escapeHTML(created)}</td>
        <td class="admin-actions-col">
          <button class="btn btn-danger-soft btn-sm" data-delete-user="${escapeHTML(u.id)}" data-name="${escapeHTML(u.name || u.email || "this user")}">Delete</button>
        </td>
      </tr>
    `;
  }).join("");

  wrap.innerHTML = `
    <table class="admin-table">
      <thead>
        <tr>
          <th>Workspace</th>
          <th>Email</th>
          <th>Role</th>
          <th>Created</th>
          <th></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  wrap.querySelectorAll("[data-delete-user]").forEach((btn) => {
    btn.addEventListener("click", () => deleteAdminUser(btn.dataset.deleteUser, btn.dataset.name));
  });
}

async function deleteAdminUser(userId, displayName) {
  if (!userId) return;
  if (!confirm(`Delete account "${displayName}"? This cannot be undone.`)) return;
  try {
    const res = await fetch(`${API}/api/admin/users/${encodeURIComponent(userId)}`, {
      method: "DELETE",
      headers: headers(),
    });
    const data = await parseApiResponse(res);
    if (!res.ok) throw new Error(data.detail || "Failed to delete user");
    toast("User deleted", { kind: "success" });
    loadAdminUsers();
  } catch (err) {
    toast(err.message, { kind: "error" });
  }
}

document.querySelectorAll('button[data-admin-view]').forEach((btn) => {
  btn.addEventListener("click", () => setAdminView(btn.dataset.adminView));
});

const btnAdminRefresh = $("btn-admin-refresh");
if (btnAdminRefresh) btnAdminRefresh.addEventListener("click", loadAdminUsers);

const btnAdminGotoCreate = $("btn-admin-goto-create");
if (btnAdminGotoCreate) btnAdminGotoCreate.addEventListener("click", () => setAdminView("create"));

const adminCreateForm = $("admin-create-form");
if (adminCreateForm) {
  adminCreateForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msgEl = $("admin-create-msg");
    if (msgEl) msgEl.classList.add("hidden");
    const submitBtn = $("admin-create-submit");
    setLoading(submitBtn, true);
    const body = {
      name: $("admin-new-name").value.trim(),
      email: $("admin-new-email").value.trim(),
      password: $("admin-new-password").value,
      is_admin: $("admin-new-is-admin").checked,
    };
    try {
      const res = await fetch(`${API}/api/admin/users`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(body),
      });
      const data = await parseApiResponse(res);
      if (!res.ok) throw new Error(data.detail || "Could not create user");
      toast(`Account created for ${data.email}`, { kind: "success" });
      adminCreateForm.reset();
      setAdminView("users");
    } catch (err) {
      if (msgEl) {
        msgEl.textContent = err.message;
        msgEl.classList.remove("hidden");
      }
      toast(err.message, { kind: "error" });
    } finally {
      setLoading(submitBtn, false);
    }
  });
}

/* ----------------- BOOT ----------------- */

if (apiKey) {
  if (isAdminUser) {
    showAdmin();
  } else {
    showDash();
  }
} else {
  showAuth();
}
