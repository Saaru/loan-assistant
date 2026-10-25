"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const secs = (ms) => (ms == null ? "–" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);

const store = {
  get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* storage unavailable */ } },
};

const state = {
  config: null, customers: [], session: null, workspace: "loan_servicing",
  busy: false, cards: new Map(), lastTrace: null, opsTimer: null,
};

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.session) headers.Authorization = `Bearer ${state.session.token}`;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { signOut(); throw new Error("Session expired"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Start-up and sign-in
// ---------------------------------------------------------------------------
async function init() {
  state.config = await api("/api/config");
  $("#brand-name").textContent = state.config.brand;
  document.title = `${state.config.brand} Assistant`;
  $("#footnote").textContent = `${state.config.assistant} uses live data from the core systems. ` +
    (state.config.llm_configured ? `Model: ${state.config.model}.` : "No model configured: add GROQ_API_KEY (free) to .env and restart.");
  renderTabs();
  // Deep link for demos: /?customer=CUST1001&workspace=card_servicing
  const params = new URLSearchParams(location.search);
  if (params.get("customer")) {
    history.replaceState(null, "", location.pathname);
    return signIn(params.get("customer"), params.get("workspace") || state.workspace);
  }
  const saved = store.get("la-session");
  if (saved) {
    state.session = saved;
    state.workspace = saved.usecase.id;
    renderTabs();
    onSignedIn(false);
  } else {
    showSignIn();
  }
}

function renderTabs() {
  const tabs = $("#workspace-tabs");
  tabs.innerHTML = "";
  for (const uc of state.config.usecases) {
    const b = h(`<button class="tab ${uc.id === state.workspace ? "active" : ""}" title="${esc(uc.tagline)}">${esc(uc.name)}</button>`);
    b.onclick = () => switchWorkspace(uc.id);
    tabs.append(b);
  }
}

async function showSignIn() {
  document.body.classList.add("signed-out");
  $("#suggestions").innerHTML = "";
  $("#signin").hidden = false;
  $("#signin-title").textContent = `Sign in to ${state.config.brand}`;
  const box = $("#customers");
  box.innerHTML = `<div class="muted">Loading demo customers…</div>`;
  try {
    state.customers = await api("/api/customers");
  } catch (e) {
    box.innerHTML = `<div class="error-bubble">Couldn't reach the CRM: ${esc(e.message)}. Retrying…</div>`;
    setTimeout(showSignIn, 2000);
    return;
  }
  box.innerHTML = "";
  for (const c of state.customers) {
    const el = h(`<button class="customer"><div class="n">${esc(c.full_name)}</div>
      <div class="muted small">${esc(c.customer_id)} · ${esc(c.city)} · ${esc(c.segment)}</div>
      <div class="s">${esc(c.demo_story || "")}</div></button>`);
    el.onclick = () => signIn(c.customer_id);
    box.append(el);
  }
}

async function signIn(customerId, workspace = state.workspace) {
  const res = await api("/api/session", { method: "POST", body: JSON.stringify({ customer_id: customerId, usecase: workspace }) });
  state.session = res;
  state.workspace = workspace;
  store.set("la-session", res);
  renderTabs();
  onSignedIn(true);
}

function signOut() {
  if (state.voice?.active) stopVoice();
  state.session = null;
  store.del("la-session");
  $("#who").hidden = true;
  $("#thread").innerHTML = "";
  $("#panels").innerHTML = "";
  showSignIn();
}

// Same conversation, different workspace: the tab changes the panels and the tools for the next question.
async function switchWorkspace(id) {
  if (id === state.workspace || state.busy) return;
  if (!state.session) { state.workspace = id; renderTabs(); return; }
  const res = await api("/api/session/workspace", { method: "POST", body: JSON.stringify({ usecase: id }) });
  applyWorkspace(res.usecase, false);
}

function applyWorkspace(usecase, byAssistant) {
  if (usecase.id === state.workspace) return;
  state.workspace = usecase.id;
  state.session.usecase = usecase;
  store.set("la-session", state.session);
  renderTabs();
  renderSuggestions(!$("#suggestions").hidden);
  loadAccount();
  $("#thread").append(h(`<div class="divider"><span>${byAssistant ? `${esc(state.config.assistant)} switched to` : "Now in"} ${esc(usecase.name)}</span></div>`));
  scrollDown(true);
}

function onSignedIn(fresh) {
  document.body.classList.remove("signed-out");
  $("#signin").hidden = true;
  const name = state.session.customer.name;
  $("#who").hidden = false;
  $("#who-name").textContent = name;
  $("#who-avatar").textContent = name.split(" ").map((p) => p[0]).join("").slice(0, 2);
  $("#thread").innerHTML = "";
  state.cards.clear();
  addWelcome(name.split(" ")[0], fresh);
  renderSuggestions();
  loadAccount();
  $("#input").focus();
}

function addWelcome(first) {
  const areas = state.config.usecases.map((u) => u.name.toLowerCase()).join(" and ");
  const msg = assistantShell();
  msg.content.innerHTML = `<p>Hi ${esc(first)}, I'm ${esc(state.config.assistant)}. I can help with ${esc(areas)}: explain what changed and why, run the numbers for you, and raise requests. What would you like to do?</p>`;
}

function renderSuggestions(show = true) {
  const tagline = state.session.usecase.tagline;
  const lead = /^[A-Z]{2,}/.test(tagline) ? tagline : tagline.charAt(0).toLowerCase() + tagline.slice(1);  // keep "EMIs"
  $("#input").placeholder = `Ask about ${lead}...`;
  const box = $("#suggestions");
  box.hidden = !show;
  box.innerHTML = "";
  for (const p of state.session.usecase.suggested_prompts || []) {
    const b = h(`<button class="suggestion">${esc(p)}</button>`);
    b.onclick = () => send(p);
    box.append(b);
  }
}

// ---------------------------------------------------------------------------
// Account panels
// ---------------------------------------------------------------------------
async function loadAccount() {
  if (!state.session) return;
  const box = $("#panels");
  if (!box.children.length) box.innerHTML = `<div class="panel muted">Loading your accounts…</div>`;
  let data;
  try { data = await api("/api/account"); } catch (e) { box.innerHTML = `<div class="panel error-bubble">${esc(e.message)}</div>`; return; }
  $("#as-of").textContent = `as of ${data.as_of}`;
  box.innerHTML = "";
  for (const [name, panel] of Object.entries(data.panels)) {
    const render = PANELS[name];
    if (!render) continue;
    if (panel.error) { box.append(h(`<div class="panel"><h3>${esc(name)}</h3><div class="muted small">Temporarily unavailable</div></div>`)); continue; }
    const el = render(panel);
    if (el) box.append(el);
  }
}

const statusChip = (s) => {
  const map = { SUCCESS: "ok", FAILED: "bad", OPEN: "warn", RESOLVED: "ok", CLOSED: "neutral", APPROVED: "ok", DECLINED: "bad", ACTIVE: "ok", INACTIVE: "bad" };
  return `<span class="chip ${map[s] || "neutral"}">${esc(String(s).replace(/_/g, " ").toLowerCase())}</span>`;
};

const PANELS = {
  profile: (p) => h(`<div class="panel"><div class="profile-name">${esc(p.name)}</div>
    <div class="muted small" style="margin-bottom:8px">${esc(p.segment)} · ${esc(p.city)} · customer since ${esc(p.customer_since)}</div>
    <dl class="kv"><dt>Mobile</dt><dd>${esc(p.mobile)}</dd><dt>Email</dt><dd>${esc(p.email)}</dd><dt>KYC</dt><dd>${statusChip(p.kyc_status === "VERIFIED" ? "ACTIVE" : "INACTIVE").replace("active", "verified")}</dd></dl></div>`),
  loans: (p) => {
    const el = h(`<div class="panel"><h3>Loans</h3></div>`);
    if (!p.items.length) el.append(h(`<div class="muted small">No loans</div>`));
    for (const l of p.items) {
      const pctDone = Math.round((l.emis_paid / l.emis_total) * 100);
      el.append(h(`<div class="loan"><div class="loan-top"><div><div style="font-weight:600">${esc(l.product)}</div><div class="loan-id">${esc(l.loan_id)}</div></div>
        <span class="chip">${esc(l.rate)} ${esc(l.rate_type)}</span></div>
        <div class="big-num">${esc(l.outstanding)}</div><div class="muted small">principal outstanding</div>
        <div class="progress"><span style="width:${pctDone}%"></span></div>
        <dl class="kv"><dt>EMI</dt><dd>${esc(l.emi)}</dd><dt>Next due</dt><dd>${esc(l.next_due || "–")}</dd><dt>EMIs paid</dt><dd>${l.emis_paid} of ${l.emis_total}</dd>
        <dt>Auto-debit</dt><dd>${statusChip(l.mandate_status)}</dd>${l.unpaid_charges ? `<dt>Unpaid charges</dt><dd><span class="chip warn">${esc(l.unpaid_charges)}</span></dd>` : ""}</dl></div>`));
    }
    return el;
  },
  loan_payments: (p) => {
    const el = h(`<div class="panel"><h3>Recent payments</h3><div class="rows"></div></div>`);
    for (const r of p.items) {
      $(".rows", el).append(h(`<div class="row"><div><div class="amt">${esc(r.amount)}</div><div class="sub">${esc(r.date)} · ${esc(r.loan_id)} · ${esc(r.method)}</div></div>
        <div style="text-align:right">${statusChip(r.status)}${r.reason ? `<div class="sub">${esc(r.reason.replace(/_/g, " ").toLowerCase())}</div>` : ""}</div></div>`));
    }
    return el;
  },
  requests: (p) => {
    const el = h(`<div class="panel"><h3>Service requests</h3><div class="rows"></div></div>`);
    if (!p.items.length) $(".rows", el).append(h(`<div class="muted small">No requests yet</div>`));
    for (const r of p.items) {
      $(".rows", el).append(h(`<div class="row"><div><div>${esc(r.summary)}</div><div class="sub">${esc(r.request_id)} · raised ${esc(r.raised_on)}${r.due_by ? ` · due ${esc(r.due_by)}` : ""}</div></div>${statusChip(r.status)}</div>`));
    }
    return el;
  },
  cards: (p) => {
    const el = h(`<div class="panel"><h3>Credit cards</h3></div>`);
    if (!p.items.length) el.append(h(`<div class="muted small">No credit cards</div>`));
    for (const c of p.items) {
      el.append(h(`<div class="card-tile"><div style="display:flex;justify-content:space-between"><b>${esc(c.product)}</b><span class="chip ${c.intl_enabled ? "ok" : "neutral"}">intl ${c.intl_enabled ? "on" : "off"}</span></div>
        <div class="num">•••• •••• •••• ${esc(c.last4)}</div>
        <dl class="kv"><dt>Available</dt><dd>${esc(c.available)} of ${esc(c.limit)}</dd><dt>Statement</dt><dd>${esc(c.statement_balance)}</dd><dt>Min due</dt><dd>${esc(c.min_due)} by ${esc(c.due_date)}</dd></dl></div>`));
    }
    return el;
  },
  card_transactions: (p) => {
    const el = h(`<div class="panel"><h3>Recent card transactions</h3><div class="rows"></div></div>`);
    for (const t of p.items) {
      $(".rows", el).append(h(`<div class="row"><div><div>${esc(t.merchant)}</div><div class="sub">${esc(t.date)} · •${esc(t.last4)}${t.note ? ` · ${esc(t.note)}` : ""}</div></div>
        <div style="text-align:right"><div class="amt">${esc(t.amount)}</div>${t.status !== "APPROVED" ? statusChip(t.status) : ""}</div></div>`));
    }
    return el;
  },
};

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
function assistantShell() {
  const el = h(`<div class="msg assistant"><div class="bot">${esc(state.config.assistant[0])}</div><div class="bubble">
    <div class="steps"></div><div class="content"></div><div class="actions"></div><div class="meta"></div><div class="evidence-slot"></div></div></div>`);
  $("#thread").append(el);
  scrollDown();
  return { el, steps: $(".steps", el), content: $(".content", el), actions: $(".actions", el), meta: $(".meta", el), evidenceSlot: $(".evidence-slot", el) };
}

function addUser(text, spoken = false) {
  $("#thread").append(h(`<div class="msg user${spoken ? " spoken" : ""}"><div class="bubble">${spoken ? '<span class="said" title="Spoken">🎙</span>' : ""}${esc(text)}</div></div>`));
  scrollDown(true);
}

// Follow new content unless the customer has scrolled up to read something.
function scrollDown(force = false) {
  const t = $("#thread");
  if (force || t.scrollHeight - t.scrollTop - t.clientHeight < 240) t.scrollTop = t.scrollHeight;
}

function setBusy(b) {
  state.busy = b;
  $("#send").disabled = b;
  $("#input").disabled = b;
  for (const s of document.querySelectorAll(".suggestion")) s.disabled = b;
}

async function send(text) {
  text = text.trim();
  if (!text || state.busy || !state.session) return;
  addUser(text);
  $("#suggestions").hidden = true;  // give the conversation the room once it has started
  $("#input").value = "";
  autosize();
  await runTurn({ message: text });
}

async function runTurn(body) {
  return streamTurn({ url: "/api/chat", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
}

// Streams one turn's events from the server. Shared by text and voice: voice passes a hook that sees every
// event first (to speak it) and an abort signal (to stop it when the customer talks over the reply).
async function streamTurn({ url, body, headers = {}, hook = null, signal = null, lockInput = true }) {
  if (lockInput) setBusy(true);
  const view = { msg: null, text: "", steps: new Map(), evidence: [], raf: null, aborted: false, turnId: null };
  try {
    const res = await fetch(url, {
      method: "POST", body, signal,
      headers: { ...headers, Authorization: `Bearer ${state.session.token}` },
    });
    if (res.status === 401) { signOut(); return view; }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));
        if (hook) hook(view, ev);
        handleEvent(view, ev);
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      view.aborted = true;
    } else {
      ensureMsg(view).content.after(h(`<div class="error-bubble">${esc(e.message)}</div>`));
    }
  } finally {
    view.done = true;
    if (view.raf) cancelAnimationFrame(view.raf);
    view.raf = null;
    if (view.msg) {
      view.msg.content.classList.remove("cursor");
      renderText(view, true);
      if (view.aborted) view.msg.meta.prepend(h(`<span class="chip neutral">interrupted</span>`));
    }
    if (lockInput) setBusy(false);
    if (!state.voice?.active) $("#input").focus();
  }
  return view;
}

function ensureMsg(view) {
  if (!view.msg) {
    view.msg = assistantShell();
    view.msg.content.classList.add("cursor");
  }
  return view.msg;
}

function handleEvent(view, ev) {
  if (ev.type === "workspace_switched") {
    applyWorkspace(ev.usecase, true);
    return;
  }
  if (["turn_start", "stt_start", "transcript", "no_speech", "cancelled"].includes(ev.type)) {
    if (ev.type === "turn_start") view.turnId = ev.turn_id;
    if (ev.type === "cancelled") view.aborted = true;  // superseded by the customer's next request
    return;  // nothing to draw (voice handles transcripts in its hook)
  }
  const msg = ensureMsg(view);
  switch (ev.type) {
    case "tool_pending":
    case "tool_start": {
      let step = view.steps.get(ev.id);
      if (!step) {
        step = h(`<div class="step running"><span class="dot"></span><span class="label">${esc(ev.label)}…</span><span class="sys"></span><span class="ms"></span></div>`);
        view.steps.set(ev.id, step);
        msg.steps.append(step);
      }
      break;
    }
    case "tool_end": {
      const step = view.steps.get(ev.id);
      if (step) {
        step.className = `step ${ev.ok ? "done" : "failed"}`;
        $(".label", step).textContent = ev.ok ? ev.label : `${ev.label} (${ev.degraded ? "system unavailable" : "could not complete"})`;
        $(".sys", step).textContent = ev.systems?.length ? `· ${ev.systems.join(", ")}` : "";
        $(".ms", step).textContent = secs(ev.ms);
      }
      break;
    }
    case "text_delta":
      view.text += ev.text;
      if (!view.raf) view.raf = requestAnimationFrame(() => { view.raf = null; renderText(view); });
      break;
    case "action_proposed":
      msg.actions.append(actionCard(ev.action));
      break;
    case "action_updated":
      updateCard(ev.action);
      if (ev.action.status === "executed") loadAccount();
      break;
    case "evidence":
      view.evidence = ev.items || [];
      break;
    case "error":
      msg.content.after(h(`<div class="error-bubble">${esc(ev.message)}</div>`));
      break;
    case "turn_end":
      finishTurn(view, ev.metrics);
      break;
  }
  scrollDown();
}

function renderText(view, final = false) {
  if (!view.msg) return;
  const html = DOMPurify.sanitize(marked.parse((view.text || "").replace(/[\u2010\u2011]/g, "-")));
  view.msg.content.innerHTML = html.replace(/\[((?:[A-Z]{2,3}-){1,2}[A-Z0-9]*\d)\]/g, '<button class="cite" data-rule="$1">$1</button>');
  for (const b of view.msg.content.querySelectorAll(".cite")) b.onclick = () => openEvidence(view, b.dataset.rule);
  if (!final && !view.done) view.msg.content.classList.add("cursor");
}

function finishTurn(view, m) {
  const { msg } = view;
  state.lastTrace = m;
  renderTrace();
  // Collapse the tool steps into a one-line summary.
  const n = view.steps.size;
  if (n) {
    const failed = [...view.steps.values()].filter((s) => s.classList.contains("failed")).length;
    const summary = h(`<button class="steps-summary">${n} lookup${n > 1 ? "s" : ""}${failed ? ` · ${failed} failed` : ""} · ${m.counts.systems.length} system${m.counts.systems.length === 1 ? "" : "s"} ▸</button>`);
    msg.steps.hidden = true;
    summary.onclick = () => { msg.steps.hidden = !msg.steps.hidden; summary.textContent = summary.textContent.replace(/[▸▾]/, msg.steps.hidden ? "▸" : "▾"); };
    msg.steps.before(summary);
  }
  msg.meta.innerHTML = "";
  if (m.first_feedback_ms != null) {
    msg.meta.append(h(`<span class="lat" title="Time to first visible response (status or text) · time to first word · total">⚡ ${secs(m.first_feedback_ms)} · first word ${secs(m.first_token_ms)} · done ${secs(m.total_ms)}</span>`));
  }
  if (view.evidence.length) {
    const rules = view.evidence.filter((e) => e.type === "rule").length;
    const calcs = view.evidence.filter((e) => e.type === "calculation").length;
    const parts = [rules && `${rules} rule${rules === 1 ? "" : "s"}`, calcs && `${calcs} calc${calcs === 1 ? "" : "s"}`].filter(Boolean);
    const btn = h(`<button class="toggle">Sources${calcs ? " &amp; calculations" : ""}${parts.length ? ` · ${parts.join(" · ")}` : ""}</button>`);
    btn.onclick = () => toggleEvidence(view);
    msg.meta.append(btn);
  }
}

function evidencePanel(items) {
  const el = h(`<div class="evidence"></div>`);
  const data = items.filter((e) => e.type === "data");
  for (const r of items.filter((e) => e.type === "rule")) {
    el.append(h(`<div class="ev-rule" data-rule="${esc(r.id)}"><div class="t"><span class="cite">${esc(r.id)}</span> ${esc(r.title)}</div>
      <div class="x">${esc(r.text)}</div><div class="v">Policy version ${esc(r.version)}, effective ${esc(r.effective_from)}${r.change_note ? ` · ${esc(r.change_note)}` : ""}</div></div>`));
  }
  for (const c of items.filter((e) => e.type === "calculation")) {
    const rows = (obj) => Object.entries(obj).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
    el.append(h(`<div class="ev-calc"><div class="t">${esc(c.title)}</div><table>${rows(c.inputs)}${rows(c.outputs)}</table>
      <div class="m">${esc(c.method)}${c.notes?.length ? `<br>${c.notes.map(esc).join("<br>")}` : ""}</div></div>`));
  }
  if (data.length) {
    el.append(h(`<div><div class="muted small" style="margin-bottom:4px">Data from</div><div class="ev-sources">${data.map((d) => `<span class="chip neutral">${esc(d.system)}: ${esc(d.label)}</span>`).join("")}</div></div>`));
  }
  return el;
}

function toggleEvidence(view) {
  const slot = view.msg.evidenceSlot;
  if (slot.firstChild) { slot.innerHTML = ""; return; }
  slot.append(evidencePanel(view.evidence));
}

function openEvidence(view, ruleId) {
  if (!view.msg.evidenceSlot.firstChild) toggleEvidence(view);
  const el = view.msg.evidenceSlot.querySelector(`[data-rule="${CSS.escape(ruleId)}"]`);
  if (el) { el.scrollIntoView({ block: "nearest", behavior: "smooth" }); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
}

// ---------------------------------------------------------------------------
// Action confirmation cards
// ---------------------------------------------------------------------------
function actionCard(a) {
  const el = h(`<div class="action-card" data-id="${esc(a.action_id)}"></div>`);
  state.cards.set(a.action_id, el);
  paintCard(el, a);
  return el;
}

function updateCard(a) {
  const el = state.cards.get(a.action_id);
  if (el) paintCard(el, a);
}

function paintCard(el, a, error) {
  el.className = `action-card ${a.status}`;
  const rows = a.summary.map((r) => `<dt>${esc(r.label)}</dt><dd>${esc(r.value)}</dd>`).join("");
  const label = { pending: "Awaiting your confirmation", executing: "Processing…", executed: "Done", failed: "Couldn't complete", cancelled: "Cancelled", expired: "Expired" }[a.status] || a.status;
  el.innerHTML = `<div class="ah"><span>${esc(a.title)}</span><span class="chip ${a.status === "executed" ? "ok" : a.status === "pending" ? "warn" : "neutral"}">${esc(label)}</span></div>
    <div class="ab"><dl class="kv">${rows}</dl><div class="controls"></div></div>`;
  const controls = $(".controls", el);
  if (a.status === "pending" || a.status === "failed") {
    const otp = a.requires_otp ? `<input inputmode="numeric" maxlength="6" placeholder="OTP" aria-label="One-time password">` : "";
    const hint = a.requires_otp ? `<div class="otp-hint">OTP sent to ${esc(a.otp_destination)}${a.demo_otp ? ` · demo OTP: <b>${esc(a.demo_otp)}</b>` : ""}. You can also type it in the chat.</div>` : "";
    controls.append(h(`<div class="otp-row">${hint}${otp}<button class="btn confirm">${a.status === "failed" ? "Retry" : "Confirm"}</button><button class="btn ghost cancel">Cancel</button></div>`));
    $(".confirm", controls).onclick = () => confirmCard(el, a);
    $(".cancel", controls).onclick = () => cancelCard(el, a);
    const input = $("input", controls);
    if (input) input.onkeydown = (e) => { if (e.key === "Enter") confirmCard(el, a); };
  }
  if (a.status === "executed" && a.result) {
    const r = a.result;
    controls.append(h(`<div class="done">✓ ${esc(r.request_id ? `Request ${r.request_id}` : "Completed")}${r.status ? ` · ${esc(String(r.status).toLowerCase())}` : ""}${r.due_by ? ` · due ${esc(r.due_by)}` : ""}</div>`));
  }
  if (error || a.error) controls.append(h(`<div class="err">${esc(error || a.error)}</div>`));
}

async function confirmCard(el, a) {
  if (state.busy) return;
  const otp = $("input", el)?.value?.trim();
  if (a.requires_otp && !otp) { paintCard(el, a, "Enter the OTP to confirm."); return; }
  for (const b of el.querySelectorAll("button")) b.disabled = true;
  const res = await api(`/api/actions/${a.action_id}/confirm`, { method: "POST", body: JSON.stringify({ otp }) });
  paintCard(el, res.action || a, res.ok ? null : res.error);
  if (res.ok) {
    loadAccount();
    await runTurn({ event: { type: "action_completed", action_id: a.action_id } });
  }
}

async function cancelCard(el, a) {
  if (state.busy) return;
  const res = await api(`/api/actions/${a.action_id}/cancel`, { method: "POST" });
  if (res.action) paintCard(el, res.action);
  await runTurn({ event: { type: "action_cancelled", action_id: a.action_id } });
}

// ---------------------------------------------------------------------------
// Ops drawer
// ---------------------------------------------------------------------------
function toggleOps(open) {
  const ops = $("#ops");
  ops.hidden = !open;
  $("#layout").classList.toggle("with-ops", open);
  clearInterval(state.opsTimer);
  if (open) { refreshOps(); state.opsTimer = setInterval(refreshOps, 4000); }
}

async function refreshOps() {
  let data;
  try { data = await api("/api/ops"); } catch { return; }
  const box = $("#systems");
  box.innerHTML = "";
  if (data.systems.error) { box.textContent = data.systems.error; return; }
  for (const [name, s] of Object.entries(data.systems.systems)) {
    const breaker = data.breakers[name]?.breaker || "closed";
    const row = h(`<div class="sys-row"><div><div>${esc(s.display_name)}</div><div class="muted small">p50 ${s.profile.latency_ms.p50} ms · ${Math.round((s.profile.error_rate || 0) * 100)}% errors${s.profile.extra_latency_ms ? ` · +${s.profile.extra_latency_ms} ms` : ""}</div></div>
      <span class="chip ${breaker === "closed" ? "ok" : "bad"}" title="Circuit breaker">${esc(breaker)}</span>
      <select>${data.systems.modes.map((m) => `<option ${m === s.mode ? "selected" : ""}>${esc(m)}</option>`).join("")}</select></div>`);
    $("select", row).onchange = async (e) => { await api("/api/ops/chaos", { method: "POST", body: JSON.stringify({ system: name, mode: e.target.value }) }); refreshOps(); };
    box.append(row);
  }
  renderMetrics(data.metrics);
}

function renderMetrics(m) {
  const row = (label, s) => `<tr><td>${esc(label)}</td><td>${s?.n ?? 0}</td><td>${secs(s?.p50)}</td><td>${secs(s?.p95)}</td></tr>`;
  let html = `<table class="metric-table"><tr><th></th><th>n</th><th>p50</th><th>p95</th></tr>
    ${row("First feedback", m.first_feedback_ms)}${row("First word", m.first_token_ms)}${row("Full answer", m.total_ms)}${row("LLM round", m.llm_round_ms)}`;
  const v = m.voice || {};
  if (v.speech_to_text_ms?.n) {
    html += row("Voice: speech-to-text", v.speech_to_text_ms) + row("Voice: acknowledgement", v.end_of_speech_to_first_audio_ms)
      + row("Voice: answer audio", v.end_of_speech_to_answer_audio_ms);
  }
  for (const [sys, s] of Object.entries(m.systems || {})) html += row(`↳ ${sys}${s.failed_attempts ? ` (${s.failed_attempts} failed)` : ""}`, s);
  html += `</table><div class="muted small" style="margin-top:6px">${m.turns} turns · backend cache hit rate ${Math.round((m.cache_hit_rate || 0) * 100)}%</div>`;
  $("#metrics").innerHTML = html;
}

function renderTrace() {
  const m = state.lastTrace;
  const box = $("#trace");
  if (!m) { box.innerHTML = `<div class="muted small">Ask something to see the trace.</div>`; return; }
  const total = m.total_ms || 1;
  const spans = m.spans.filter((s) => !(s.kind === "backend" && s.attrs.cache_hit));
  let html = `<div class="legend"><span><i style="background:#6c7ae0"></i>LLM</span><span><i style="background:var(--accent)"></i>tool</span><span><i style="background:#d99a2b"></i>backend</span></div><div class="wf">`;
  for (const s of spans) {
    const left = (s.start_ms / total) * 100, width = Math.max((s.duration_ms / total) * 100, 0.4);
    const name = s.kind === "backend" ? `${s.attrs.system}${s.attrs.attempt > 1 ? ` #${s.attrs.attempt}` : ""}` : s.name;
    html += `<div class="wf-row"><span class="name" title="${esc(s.name)}">${s.kind === "backend" ? "  " : ""}${esc(name)}</span><span class="wf-track"><span class="wf-bar ${s.ok ? s.kind : "fail"}" style="left:${left}%;width:${width}%"></span></span><span class="d">${secs(s.duration_ms)}</span></div>`;
  }
  html += `</div><div class="muted small" style="margin-top:6px">Total ${secs(m.total_ms)} · ${m.counts.llm_rounds} LLM round(s) · ${m.usage.input_tokens + m.usage.cache_read_input_tokens} input tokens (${m.usage.cache_read_input_tokens} cached) · ${m.usage.output_tokens} output</div>`;
  box.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function autosize() {
  const t = $("#input");
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight, 140)}px`;
}

$("#composer").onsubmit = (e) => { e.preventDefault(); send($("#input").value); };
$("#input").addEventListener("input", autosize);
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("#input").value); } });
$("#switch-customer").onclick = signOut;
$("#ops-toggle").onclick = () => toggleOps($("#ops").hidden);
$("#ops-close").onclick = () => toggleOps(false);
$("#reset-demo").onclick = async () => {
  if (!confirm("Reset all demo data and sign out?")) return;
  await api("/api/ops/reset", { method: "POST" });
  signOut();
};

init().catch((e) => { document.body.prepend(h(`<div class="error-bubble" style="margin:12px">Couldn't start: ${esc(e.message)}</div>`)); });
