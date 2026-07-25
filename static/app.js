const App = {
  state: "clients",
  clients: [],
  client: null,
  plan: null,
  result: null,
  error: null,
  mediaRecorder: null,
  chunks: [],
  seconds: 0,
  timerId: null,
  audioCtx: null,
  analyser: null,
  raf: null,
  procTimers: [],
  recMode: "debrief",
  assistant: null,
  // Records UI
  nav: "home",            // sidebar highlight: home | client:<id> | lib:worksheets | lib:reference | trash
  recordData: null,        // GET /api/clients/{id} payload
  recordTab: "sessions",  // sessions | profile | documents
  doc: null,               // current document view payload
  docCrumb: null,          // breadcrumb context for the document view
  library: null,           // GET /api/library payload
  trash: null,             // GET /api/trash items
  searchTimer: null,
  overflowOpen: false,
  // Setup wizard
  status: null,            // GET /api/status payload
  setupPerms: null,        // { calendar, mail, screen } permission results
  checkWasFailing: null,   // presentation only: which setup checks failed before the last Re-check
  settings: null,          // GET /api/settings -> settings object (features, etc.)
  settingsPayload: null,   // full GET /api/settings payload (settings, dictionary, professions, formats)
  wizard: null,            // in-progress onboarding choices, POSTed before setup/complete
};

// Mirror of settings_store DEFAULTS: used when /api/settings has not loaded yet
// so feature gates default to all-on and never hide UI by accident.
const SETTINGS_DEFAULTS = {
  profession: "therapy",
  note_format: "DAP",
  features: { calendar: true, email: true, verify: true, assistant: true },
  stt_engine: "parakeet",
};

let el = null;             // reassigned each render to the active container
let stream = null;

const FLOW_STATES = new Set([
  "record", "processing", "review", "executing", "results",
  "assistant", "assistantThinking", "assistantReview", "assistantResults",
  "setupWizard",
]);

function h(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;" }[c])); }
function fmtSecs(n) { const m = Math.floor(n/60), s = n%60; return `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`; }

function fmtClock(d) {
  let h = d.getHours() % 12 || 12;
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${h}:${mm} ${d.getHours() >= 12 ? "PM" : "AM"}`;
}
function fmtDateTimeDisplay(d) {
  const day = d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  return `${day} at ${fmtClock(d)}`;
}

// Pure, deterministic gap detection over the plan JSON. No LLM involved.
// Returns a list of nudge descriptors; rendering decides how each looks.
function computeNudges(plan) {
  const acts = (plan && plan.actions) || [];
  const nudges = [];
  if (!acts.some(a => a.type === "schedule_followup")) {
    nudges.push({ id: "no-followup", text: "No follow-up booked.", sub: "Dictate again or add one below." });
  }
  if (!acts.some(a => a.type === "draft_client_email")) {
    nudges.push({ id: "no-email", text: "No client email requested." });
  }
  const unsupported = (plan && plan.unsupported_requests) || [];
  if (unsupported.length) {
    nudges.push({ id: "unsupported", text: "Heard but can't do yet: " + unsupported.join("; ") });
  }
  if (plan && plan.note && plan.note.risk_present) {
    nudges.push({ id: "risk", text: "Risk content documented.", sub: "Review the Risk section before executing." });
  }
  return nudges;
}
window.computeNudges = computeNudges;

function addManualFollowup(dateVal, timeVal, durVal) {
  const d = new Date(`${dateVal}T${timeVal}:00`);
  if (isNaN(d.getTime())) return;
  const first = (App.plan.client && App.plan.client.first_name) || "Client";
  const duration = Math.max(5, parseInt(durVal, 10) || 50);
  const iso = `${dateVal}T${timeVal}:00`;
  App.plan.actions.push({
    type: "schedule_followup",
    datetime_utterance: "manually added",
    resolved_datetime: iso,
    datetime_display: fmtDateTimeDisplay(d),
    duration_min: duration,
    title: `${first} ${fmtClock(d)} session`,
    label: `Book follow-up: ${fmtDateTimeDisplay(d)} (${duration} min)`,
    enabled: true,
  });
  render();
}

function addWorksheetEmail() {
  App.plan.actions.push({
    type: "draft_client_email",
    purpose: "confirmation and homework",
    attachment: "thought record worksheet",
    attachment_name: "thought record worksheet",
    label: "Draft confirmation email with the thought record worksheet",
    enabled: true,
  });
  render();
}

// Profession -> default note format. Mirrors the plan's onboarding rule: the
// first clinical format for clinical professions, GROW for coaching, a meeting
// memo for legal meetings. Used when the profession changes in the wizard.
const PROF_DEFAULT_FORMAT = { therapy: "DAP", slp: "DAP", coaching: "GROW", legal_meeting: "meeting-memo" };

// Fallbacks used only when /api/settings could not be fetched, so the wizard and
// settings screen still render selects instead of breaking. Names match vocab.py
// and formats.py so the picker labels are correct even offline.
const PROFESSIONS_FALLBACK = [
  { id: "therapy", name: "Therapy", clinical: true },
  { id: "slp", name: "Speech-Language Pathology", clinical: true },
  { id: "coaching", name: "Coaching", clinical: false },
  { id: "legal_meeting", name: "Legal Meeting", clinical: false },
];
const FORMATS_FALLBACK = [
  { id: "DAP", name: "DAP note", clinical: true },
  { id: "SOAP", name: "SOAP note", clinical: true },
  { id: "GROW", name: "GROW model", clinical: false },
  { id: "meeting-memo", name: "Meeting memo", clinical: false },
];

function settingsFallbackPayload() {
  return { settings: SETTINGS_DEFAULTS, dictionary: "", professions: PROFESSIONS_FALLBACK, formats: FORMATS_FALLBACK };
}

async function refreshSettings() {
  try {
    const r = await fetch("/api/settings");
    if (r.ok) {
      const payload = await r.json();
      App.settingsPayload = payload;
      App.settings = payload.settings || SETTINGS_DEFAULTS;
      return;
    }
  } catch (e) { /* fall through to defaults */ }
  App.settingsPayload = settingsFallbackPayload();
  App.settings = SETTINGS_DEFAULTS;
}

async function loadClients() {
  // Load clients and settings in parallel; settings gate feature UI (e.g. the
  // assistant sidebar item) so it must be ready before the first render.
  const settingsReady = refreshSettings();
  try {
    const r = await fetch("/api/clients");
    if (!r.ok) throw new Error("Could not load clients (" + r.status + ")");
    App.clients = await r.json();
  } catch (e) { App.error = e.message; }
  await settingsReady;
  // First run: if the setup marker is absent and we were opened at the root
  // (not a deep link), show the setup wizard. Deep links are never hijacked.
  if (!location.hash) {
    await refreshStatus();
    if (App.status && App.status.first_run) { go("setupWizard"); return; }
  }
  if (!applyHash()) render();
}

async function refreshStatus() {
  try {
    const r = await fetch("/api/status");
    if (r.ok) App.status = await r.json();
  } catch (e) { /* status is best-effort; the app still works without it */ }
}

// Lightweight deep links so a record, document, or library view is bookmarkable
// and shareable within the app. Never a security surface: paths still validate
// server-side. Forms: #/client/<id>, #/note/<encoded path>, #/library/<which>, #/trash
function applyHash() {
  const hash = location.hash || "";
  const client = hash.match(/^#\/client\/([^/]+)$/);
  if (client) { const c = App.clients.find(x => x.client_id === client[1]); if (c) { openClient(c); return true; } }
  const note = hash.match(/^#\/note\/(.+)$/);
  if (note) {
    const path = decodeURIComponent(note[1]);
    const cm = path.match(/^Clients\/([^/]+)\//);
    if (cm) { const c = App.clients.find(x => x.client_id === cm[1]); if (c) App.client = c; }
    openDocument(path, { client: App.recordData, section: "Sessions", title: "" });
    return true;
  }
  const lib = hash.match(/^#\/library\/(worksheets|reference)$/);
  if (lib) { openLibrary(lib[1]); return true; }
  if (hash === "#/trash") { openTrash(); return true; }
  return false;
}
window.addEventListener("hashchange", applyHash);

function clearProcTimers() { (App.procTimers || []).forEach(clearTimeout); App.procTimers = []; }

function go(state) {
  clearProcTimers();
  App.overflowOpen = false;
  App.state = state;
  App.error = null;
  if (state === "assistant") App.nav = "assistant";
  else if (state === "setupWizard") App.nav = "setup";
  else if (state === "settings") App.nav = "settings";
  else if (!["clientRecord", "document", "library", "trash"].includes(state)) App.nav = "home";
  render();
}

const DISPATCH = {
  clients: renderClients, record: renderRecord, processing: renderProcessing,
  review: renderReview, executing: renderExecuting, results: renderResults,
  assistant: renderAssistant, assistantThinking: renderAssistantThinking,
  assistantReview: renderAssistantReview, assistantResults: renderAssistantResults,
  clientRecord: renderClientRecord, document: renderDocument,
  library: renderLibrary, trash: renderTrash,
  setupWizard: renderSetupWizard, settings: renderSettings,
};

// ---------------------------------------------------------------------------
// Persistent shell: sidebar + main pane. Built once; the main pane swaps.
// ---------------------------------------------------------------------------

const LOCK_DOT = `<span class="dot"></span>`;

function initials(name) {
  return (name || "?").trim().split(/\s+/).map(w => w[0]).slice(0, 2).join("").toUpperCase();
}

function ensureShell() {
  const shell = document.getElementById("shell");
  if (shell.querySelector(".app-shell")) return;
  shell.innerHTML = `
    <div class="app-shell">
      <aside class="side">
        <div class="brand">Debrief</div>
        <div class="side-search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>
          <input type="search" id="globalSearch" placeholder="Search records" autocomplete="off" />
          <div id="searchResults"></div>
        </div>
        <button class="navitem" id="navNewDebrief"><span class="nav-ic">🎙</span> New debrief</button>
        <button class="navitem" id="navAssistant"><span class="nav-ic">✦</span> Ask the assistant</button>
        <div class="nav-sec">Clients</div>
        <div id="navClients"></div>
        <div class="nav-sec">Library</div>
        <button class="navitem" id="navWorksheets"><span class="nav-ic">📄</span> Worksheets</button>
        <button class="navitem" id="navReference"><span class="nav-ic">📚</span> Reference</button>
        <button class="navitem" id="navTrash"><span class="nav-ic">🗑</span> Trash</button>
        <div class="side-tail">
          <button class="navitem navitem-setup" id="navSettings"><span class="nav-ic">⚙</span> Settings</button>
          <button class="navitem navitem-setup" id="navSetup"><span class="nav-ic">✦</span> Setup</button>
          <div class="side-foot">${LOCK_DOT} Data secure on this Mac</div>
        </div>
      </aside>
      <main class="main-pane" id="main-pane"></main>
    </div>`;
  shell.querySelector("#navNewDebrief").onclick = () => { App.client = null; go("clients"); };
  shell.querySelector("#navAssistant").onclick = () => { App.assistant = null; go("assistant"); };
  shell.querySelector("#navWorksheets").onclick = () => openLibrary("worksheets");
  shell.querySelector("#navReference").onclick = () => openLibrary("reference");
  shell.querySelector("#navTrash").onclick = () => openTrash();
  shell.querySelector("#navSettings").onclick = () => openSettings();
  shell.querySelector("#navSetup").onclick = () => openSetup();
  const search = shell.querySelector("#globalSearch");
  search.oninput = () => runSearch(search.value);
  search.onkeydown = (e) => { if (e.key === "Escape") { search.value = ""; runSearch(""); } };
  // Feature gate: hide the assistant entry when it is turned off in settings.
  if (!assistantEnabled()) {
    const a = shell.querySelector("#navAssistant");
    if (a) a.style.display = "none";
  }
  renderSidebarClients();
}

function assistantEnabled() {
  const feats = (App.settings && App.settings.features) || {};
  return feats.assistant !== false;
}

function renderSidebarClients() {
  const box = document.getElementById("navClients");
  if (!box) return;
  box.innerHTML = "";
  const styles = ["", "alt", "alt2"];
  App.clients.forEach((c, i) => {
    const item = h(`<button class="navitem"><span class="mono ${styles[i % 3]}">${esc(initials(c.name))}</span> ${esc(c.name)}</button>`);
    item.onclick = () => openClient(c);
    box.appendChild(item);
  });
}

function updateNav() {
  const map = {
    home: "navNewDebrief",
    assistant: "navAssistant",
    "lib:worksheets": "navWorksheets",
    "lib:reference": "navReference",
    trash: "navTrash",
    setup: "navSetup",
    settings: "navSettings",
  };
  document.querySelectorAll(".navitem").forEach(n => n.classList.remove("on"));
  const id = map[App.nav];
  if (id) { const node = document.getElementById(id); if (node) node.classList.add("on"); }
  if (App.nav && App.nav.startsWith("client:")) {
    const cid = App.nav.slice(7);
    const idx = App.clients.findIndex(c => c.client_id === cid);
    if (idx >= 0) {
      const nodes = document.querySelectorAll("#navClients .navitem");
      if (nodes[idx]) nodes[idx].classList.add("on");
    }
  }
}

// ---------------------------------------------------------------------------
// Entrance animation gating (presentation only).
// A screen enters as a few semantic chunks that fade and rise ~70ms apart. That
// should happen when you arrive at a screen, never when the same screen repaints
// underneath you (adding a follow-up, re-checking setup, saving a setting), so
// the classes are only emitted when the screen key changes.
// ---------------------------------------------------------------------------
let enterKeyLast = null;
let enterOn = false;
// Arriving at a screen opens a short window in which the classes are still
// emitted. Screens that paint once and then repaint the moment their data
// lands (settings, the wizard) both fetch in well under 30ms, so the second
// paint restarts an animation that has barely begun and the entrance survives.
let enterUntil = 0;
const ENTER_WINDOW_MS = 250;

function enterKey() {
  // Screens that paint a spinner first and their content second count the two
  // as different keys, so the content still gets its entrance.
  if (App.state === "setupWizard") return "setupWizard:" + (App.status ? "ready" : "loading");
  if (App.state === "settings") return "settings:" + (App.settingsPayload ? "ready" : "loading");
  return App.state;
}

// Classes for one staggered chunk, or "" when this render must not animate.
function ent(step) {
  if (!enterOn) return "";
  const n = Math.min(Math.max(Math.round(step) || 1, 1), 8);
  return "enter d" + n;
}

function render() {
  ensureShell();
  renderSidebarClients();
  updateNav();
  const key = enterKey();
  const now = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
  if (key !== enterKeyLast) { enterKeyLast = key; enterUntil = now + ENTER_WINDOW_MS; }
  enterOn = now < enterUntil;
  const pane = document.getElementById("main-pane");
  pane.innerHTML = "";
  if (FLOW_STATES.has(App.state)) {
    el = h(`<div class="flow-wrap"></div>`);
    pane.appendChild(el);
  } else {
    el = pane;
  }
  if (App.error && (App.state === "clients" || App.state === "record")) {
    el.appendChild(h(`<div class="banner banner-error">${esc(App.error)}</div>`));
  }
  (DISPATCH[App.state] || renderClients)();
}

function renderClients() {
  el.appendChild(h(`<div class="step-title ${ent(1)}">Choose a client to debrief</div>`));
  const grid = h(`<div class="clients"></div>`);
  if (!App.clients.length) grid.appendChild(h(`<div class="hint">No clients found in the vault.</div>`));
  App.clients.forEach((c, i) => {
    const risk = (c.risk_flags && c.risk_flags.length) ? `<span class="tag-risk">risk history on file</span>` : "";
    const concerns = (c.presenting_concerns || []).join(", ");
    const initials = (c.name || "?").trim().split(/\s+/).map(w => w[0]).slice(0, 2).join("").toUpperCase();
    const card = h(`<button class="client-card ${ent(i + 2)}">
      <span class="mono">${esc(initials)}</span>
      <span>
        <div class="name">${esc(c.name)}</div>
        <div class="meta">${esc(c.client_id)} &middot; ${esc(c.framework || "")}</div>
        <div class="concerns">${esc(concerns)}</div>
        ${risk}
      </span>
      <span class="card-arrow" aria-hidden="true"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 12h14M13 6l6 6-6 6"/></svg></span>
    </button>`);
    card.onclick = () => { App.client = c; go("record"); };
    grid.appendChild(card);
  });
  el.appendChild(grid);

  if (assistantEnabled()) {
    const asstRow = h(`<div class="asst-entry ${ent(5)}">
      <button class="btn btn-ghost btn-asst" id="asstEntry">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3a4 4 0 0 0-4 4v4a4 4 0 0 0 8 0V7a4 4 0 0 0-4-4z"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>
        Ask the assistant
      </button>
      <span class="asst-hint">Make a worksheet, draft an email, or look something up</span>
    </div>`);
    asstRow.querySelector("#asstEntry").onclick = () => { App.assistant = null; go("assistant"); };
    el.appendChild(asstRow);
  }
}

function renderRecord() {
  App.recMode = "debrief";
  const c = App.client;
  const bars = Array.from({ length: 24 }, () => "<i></i>").join("");
  const stage = h(`<div class="panel record-stage ${ent(1)}">
    <button class="backlink">&larr; back to clients</button>
    <div class="record-client">Debrief for <b>${esc(c.name)}</b> &middot; ${esc(c.framework || "")}</div>
    <div class="rec-wrap" id="recWrap">
      <div class="ring"></div>
      <div class="ring r2"></div>
      <button class="rec-btn" id="recBtn" aria-label="Start recording">
        <span class="icon-mic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></span>
        <span class="icon-stop"><span class="sq"></span></span>
      </button>
    </div>
    <div class="timer idle" id="timer">00:00</div>
    <div class="wave" id="wave" aria-hidden="true">${bars}</div>
    <div class="hint" id="recHint">Tap to record. Speak your note, the follow-up time, and any email you want drafted.</div>
    <div class="dictate-guide">
      <div class="dg-title">While you dictate</div>
      <ul>
        <li>What happened this session and how the client responded</li>
        <li>A risk check, if it came up</li>
        <li>When you would like the next appointment</li>
        <li>Any email to send, with resources or reminders</li>
        <li>Homework you assigned</li>
      </ul>
    </div>
  </div>`);
  stage.querySelector(".backlink").onclick = () => { stopStream(); go("clients"); };
  stage.querySelector("#recBtn").onclick = toggleRecord;
  el.appendChild(stage);
}

const CHECK_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M4 12l5 5L20 6"/></svg>`;
const LOCK_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>`;

function renderProcessing() {
  const steps = ["Transcribe the recording", "Tidy clinical terms", "Draft the DAP note", "Plan admin actions"];
  const panel = h(`<div class="panel proc-steps ${ent(1)}">
    ${steps.map((t, i) => `<div class="pstep ${i === 0 ? "active" : "todo"}"><span class="ic">${CHECK_SVG}</span><span class="t">${t}</span></div>`).join("")}
    <div class="local-note">${LOCK_SVG}Everything runs on this Mac. Nothing leaves it.</div>
  </div>`);
  el.appendChild(panel);
  // Optimistic pacing only; the real work finishes whenever the server replies.
  // Transcription dominates and scales with audio length, later steps are steadier.
  const advance = (i) => {
    if (App.state !== "processing") return;
    document.querySelectorAll(".pstep").forEach((s, j) => {
      s.classList.toggle("done", j < i);
      s.classList.toggle("active", j === i);
      s.classList.toggle("todo", j > i);
    });
  };
  const est = Math.max(4, Math.round((App.seconds || 30) * 0.35));
  clearProcTimers();
  App.procTimers = [
    setTimeout(() => advance(1), est * 1000),
    setTimeout(() => advance(2), (est + 4) * 1000),
    setTimeout(() => advance(3), (est + 4 + 8) * 1000),
  ];
}

// A click-to-edit note section. The body is a contenteditable div seeded via
// textContent (never innerHTML) and read back via innerText, so nothing the
// model produced is ever treated as markup. Edits write straight into the plan
// object the approve step POSTs verbatim: what you see is what gets filed.
function buildEditableSection(heading, key, note) {
  const wrap = h(`<div class="note-section editable">
    <h4>${esc(heading)}<span class="edit-pencil" aria-hidden="true" title="Click to edit">✎</span></h4>
    <div class="note-body" contenteditable="true" spellcheck="true" role="textbox" aria-label="${esc(heading)}"></div>
  </div>`);
  const body = wrap.querySelector(".note-body");
  body.textContent = (note && note[key] != null) ? String(note[key]) : "";
  const commit = () => { note[key] = body.innerText; };
  body.addEventListener("input", commit);
  body.addEventListener("blur", commit);
  return wrap;
}

// An editable risk field: writes back into note.risk[key] via innerText only.
function buildRiskRow(label, key, riskObj) {
  const row = h(`<div class="row"><b>${esc(label)}:</b> <span class="risk-edit" contenteditable="true" spellcheck="true" role="textbox" aria-label="${esc(label)}"></span></div>`);
  const span = row.querySelector(".risk-edit");
  span.textContent = (riskObj && riskObj[key] != null) ? String(riskObj[key]) : "";
  const commit = () => { riskObj[key] = span.innerText; };
  span.addEventListener("input", commit);
  span.addEventListener("blur", commit);
  return row;
}

function renderReview() {
  const p = App.plan, note = p.note || {};
  el.appendChild(h(`<div class="step-title ${ent(1)}">Review before anything runs</div>`));

  const risk = note.risk_present && note.risk ? note.risk : null;
  const notePanel = h(`<div class="panel doc ${ent(1)}"></div>`);
  const sd = (p.session_meta && p.session_meta.session_date) || "";
  let dateStr = "";
  if (sd) {
    const [y, m, d] = sd.split("-").map(Number);
    if (y && m && d) dateStr = new Date(y, m - 1, d).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  }
  notePanel.appendChild(h(`<div class="letterhead">
    <span class="who">${esc((p.client && p.client.name) || "Client")}</span>
    ${dateStr ? `<span class="date">${esc(dateStr)}</span>` : ""}
    <span class="stamps">
      <span class="stamp">${esc(p.client.framework || "")} ${esc((p.session_meta && p.session_meta.format) || "DAP")}</span>
      ${note.risk_present ? '<span class="stamp risk">risk documented</span>' : ""}
    </span>
  </div>`));
  notePanel.appendChild(h(`<div class="dblrule"></div>`));

  // Sections come from the active format (session_meta.sections); legacy plans
  // without them fall back to the DAP trio. Every section is click-to-edit.
  const sections = (p.session_meta && Array.isArray(p.session_meta.sections) && p.session_meta.sections.length)
    ? p.session_meta.sections
    : [{ key: "data", heading: "Data" }, { key: "assessment", heading: "Assessment" }, { key: "plan", heading: "Plan" }];
  const secBox = h(`<div class="note-sections"></div>`);
  sections.forEach(s => secBox.appendChild(buildEditableSection(s.heading, s.key, note)));
  notePanel.appendChild(secBox);

  if (risk) {
    if (!note.risk) note.risk = risk;
    const riskBox = h(`<div class="risk"><h4>Risk</h4></div>`);
    riskBox.appendChild(buildRiskRow("Ideation", "ideation", note.risk));
    riskBox.appendChild(buildRiskRow("Plan, intent, means", "plan_intent_means", note.risk));
    riskBox.appendChild(buildRiskRow("Protective factors", "protective_factors", note.risk));
    riskBox.appendChild(buildRiskRow("Interventions taken", "interventions_taken", note.risk));
    notePanel.appendChild(riskBox);
  }
  const chips = [];
  (note.interventions || []).forEach(x => chips.push(`<span class="chip">${esc(x)}</span>`));
  (note.themes || []).forEach(x => chips.push(`<span class="chip theme">${esc(x)}</span>`));
  if (chips.length) notePanel.appendChild(h(`<div class="chips">${chips.join("")}</div>`));
  notePanel.appendChild(h(`<details class="transcript"><summary>corrected transcript</summary><p>${esc(p.corrected_transcript || p.transcript || "")}</p></details>`));
  el.appendChild(notePanel);

  el.appendChild(h(`<div class="step-title ${ent(2)}">Actions to run</div>`));
  const actPanel = h(`<div class="panel ${ent(2)}"></div>`);

  // Deterministic gap nudges above the checklist. Nudges that add a disabled
  // action type (calendar booking / email drafts) are hidden: those actions are
  // turned off in settings and cannot run, so offering to add one would mislead.
  const feats = (p.session_meta && p.session_meta.features) || (App.settings && App.settings.features) || {};
  let nudges = computeNudges(p);
  if (feats.calendar === false) nudges = nudges.filter(n => n.id !== "no-followup");
  if (feats.email === false) nudges = nudges.filter(n => n.id !== "no-email");
  if (nudges.length) {
    const box = h(`<div class="nudges"></div>`);
    nudges.forEach(n => {
      const card = h(`<div class="nudge"><span class="n-text">${esc(n.text)}</span>${n.sub ? `<div class="n-sub">${esc(n.sub)}</div>` : ""}</div>`);
      if (n.id === "no-followup") {
        const addRow = h(`<div class="nudge-add">
          <input type="date" id="nfDate" aria-label="Follow-up date" />
          <input type="time" id="nfTime" value="15:00" aria-label="Follow-up time" />
          <input type="number" id="nfDur" value="50" min="5" step="5" aria-label="Duration in minutes" />
          <span class="unit">min</span>
          <button class="btn-small" id="nfAdd">Add appointment</button>
        </div>`);
        addRow.querySelector("#nfAdd").onclick = () => {
          const dv = addRow.querySelector("#nfDate").value;
          const tv = addRow.querySelector("#nfTime").value;
          if (dv && tv) addManualFollowup(dv, tv, addRow.querySelector("#nfDur").value);
        };
        card.appendChild(addRow);
      }
      if (n.id === "no-email") {
        const addRow = h(`<div class="nudge-add"><button class="btn-small" id="neAdd">Add worksheet email</button></div>`);
        addRow.querySelector("#neAdd").onclick = addWorksheetEmail;
        card.appendChild(addRow);
      }
      box.appendChild(card);
    });
    actPanel.appendChild(box);
  }

  const list = h(`<div class="actions-list"></div>`);
  if (!p.actions.length) list.appendChild(h(`<div class="a-sub" style="color:var(--ink-faint)">No admin actions were requested in this debrief.</div>`));
  p.actions.forEach((a, i) => {
    const when = a.datetime_display ? `<div class="a-when">${esc(a.datetime_display)}</div>` : "";
    const title = a.type === "schedule_followup" ? "Book follow-up appointment"
                : a.type === "draft_client_email" ? "Draft client email" : esc(a.type);
    const sub = a.type === "draft_client_email" && a.attachment_name ? `<div class="a-sub">Attaches the ${esc(a.attachment_name)}</div>`
              : a.type === "schedule_followup" && a.title ? `<div class="a-sub">Calendar title: ${esc(a.title)}</div>` : "";
    const row = h(`<label class="action on">
      <input type="checkbox" checked data-i="${i}" />
      <div class="body"><div class="a-title">${title}</div>${when}${sub}</div>
    </label>`);
    const cb = row.querySelector("input");
    cb.onchange = () => { row.classList.toggle("on", cb.checked); row.classList.toggle("off", !cb.checked); };
    list.appendChild(row);
  });
  actPanel.appendChild(list);

  if ((p.next_session_suggestions || []).length) {
    const sugg = h(`<div class="suggestions"><h4>Next session considerations</h4><ul></ul></div>`);
    const ul = sugg.querySelector("ul");
    p.next_session_suggestions.forEach(s => ul.appendChild(h(`<li>${esc(s)}</li>`)));
    actPanel.appendChild(sugg);
    actPanel.appendChild(h(`<div class="disclaimer">Clinical judgment and final session planning remain the therapist's responsibility.</div>`));
  }
  el.appendChild(actPanel);

  if ((p.errors || []).length) {
    el.appendChild(h(`<div class="banner banner-error">Some steps had trouble: ${esc(p.errors.map(e => e.stage + " (" + e.error + ")").join("; "))}</div>`));
  }

  el.appendChild(h(`<div class="edit-microcopy ${ent(3)}">Click any section to edit. Your edits are what gets filed.</div>`));

  const bar = h(`<div class="actions-bar ${ent(3)}">
    <button class="btn btn-ghost" id="redo">Discard</button>
    <div class="grow"></div>
    <button class="btn btn-primary" id="approve">Approve and run</button>
  </div>`);
  bar.querySelector("#redo").onclick = () => go("record");
  bar.querySelector("#approve").onclick = executePlan;
  el.appendChild(bar);
}

function renderExecuting() {
  el.appendChild(h(`<div class="panel processing ${ent(1)}">
    <div class="spinner"></div>
    <div class="label">Filing the note, booking, drafting, and reading the screen to verify...</div>
  </div>`));
}

function renderResults() {
  const r = App.result;
  el.appendChild(h(`<div class="step-title">Done</div>`));
  const panel = h(`<div class="panel ${ent(1)}"></div>`);
  panel.appendChild(h(`<div class="note-head"><h3>What ran</h3></div>`));
  (r.actions || []).forEach(a => {
    const title = a.type === "schedule_followup" ? "Follow-up appointment"
                : a.type === "draft_client_email" ? "Client email draft" : esc(a.type);
    panel.appendChild(h(`<div class="result-action">
      <div class="status-dot status-${esc(a.status)}"></div>
      <div><div class="r-title">${title}</div><div class="r-detail">${esc(a.detail || a.status)}</div></div>
    </div>`));
  });
  if (r.note_path) {
    panel.appendChild(h(`<div class="result-action">
      <div class="status-dot status-ok"></div>
      <div><div class="r-title">Session note filed</div><div class="r-detail">${esc(r.note_path)}</div></div>
    </div>`));
  }
  el.appendChild(panel);

  if ((r.verification || []).length) {
    el.appendChild(h(`<div class="step-title ${ent(2)}">Verified on screen</div>`));
    const vpanel = h(`<div class="${ent(2)}"></div>`);
    r.verification.forEach(v => {
      const ok = v.confirmed;
      vpanel.appendChild(h(`<div class="verify-card ${ok ? "" : "unconfirmed"}">
        <div class="verify-head"><span class="verify-surface">${esc(v.surface)}</span> &middot; ${ok ? "confirmed" : "not confirmed"}</div>
        <div class="verify-quote">${esc(v.what_i_see || "")}</div>
      </div>`));
    });
    el.appendChild(vpanel);
  }

  if ((r.errors || []).length) {
    el.appendChild(h(`<div class="banner banner-error">${esc(r.errors.map(e => e.stage + ": " + e.error).join("; "))}</div>`));
  }

  if (r.timings) {
    const t = Object.entries(r.timings).map(([k,v]) => `${k} ${v}s`).join("  &middot;  ");
    el.appendChild(h(`<div class="timings">${t}</div>`));
  }

  const bar = h(`<div class="actions-bar ${ent(3)}">
    <div class="grow"></div>
    <button class="btn btn-primary" id="another">New debrief</button>
  </div>`);
  bar.querySelector("#another").onclick = () => { App.plan = null; App.result = null; go("clients"); };
  el.appendChild(bar);
}

// ---------------------------------------------------------------------------
// Assistant (in-app agent): ask -> propose -> approve -> execute
// ---------------------------------------------------------------------------

function renderAssistant() {
  el.appendChild(h(`<div class="step-title">Ask the assistant</div>`));
  const panel = h(`<div class="panel ${ent(1)} asst-card">
    <div class="asst-lead">Ask for a worksheet, an email draft, or something to look up. Nothing is saved until you approve it.</div>
    <textarea class="asst-textarea" id="asstText" rows="3" placeholder="For example: make a one page box breathing worksheet for before meetings"></textarea>
    <div class="asst-actions">
      <button class="btn btn-ghost" id="asstMic"><span class="asst-mic-ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></span><span id="asstMicLabel">Record</span></button>
      <div class="grow"></div>
      <button class="btn btn-primary" id="asstSubmit">Ask the assistant</button>
    </div>
    <div class="local-note">${LOCK_SVG}Runs on this Mac. Nothing leaves it.</div>
  </div>`);
  panel.querySelector("#asstMic").onclick = toggleAssistantRecord;
  panel.querySelector("#asstSubmit").onclick = () => {
    const text = (document.getElementById("asstText").value || "").trim();
    if (text) submitAssistant({ text });
  };
  el.appendChild(panel);

  const bar = h(`<div class="actions-bar ${ent(2)}">
    <button class="btn btn-ghost" id="asstBack">&larr; back to clients</button>
  </div>`);
  bar.querySelector("#asstBack").onclick = () => go("clients");
  el.appendChild(bar);
}

async function toggleAssistantRecord() {
  if (App.mediaRecorder && App.mediaRecorder.state === "recording") { stopRecord();
    const l = document.getElementById("asstMicLabel"); if (l) l.textContent = "Record";
    return;
  }
  App.recMode = "assistant";
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    App.error = "Microphone access was blocked. Allow the mic and try again."; render(); return;
  }
  App.chunks = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
  App.mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
  App.mediaRecorder.ondataavailable = e => { if (e.data.size) App.chunks.push(e.data); };
  App.mediaRecorder.onstop = onRecordingStopped;
  App.mediaRecorder.start();
  const mic = document.getElementById("asstMic");
  if (mic) mic.classList.add("recording");
  const l = document.getElementById("asstMicLabel"); if (l) l.textContent = "Stop and send";
}

async function submitAssistant({ text, blob }) {
  go("assistantThinking");
  try {
    let r;
    if (blob) {
      const fd = new FormData();
      fd.append("audio", blob, "assistant.webm");
      if (App.client && App.client.client_id) fd.append("client_id", App.client.client_id);
      r = await fetch("/api/assistant/plan", { method: "POST", body: fd });
    } else {
      const payload = { text };
      if (App.client && App.client.client_id) payload.client_id = App.client.client_id;
      r = await fetch("/api/assistant/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    }
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error((t.detail && (t.detail.fix || t.detail.error || t.detail)) || ("Server error " + r.status)); }
    const data = await r.json();
    if (data.route === "session_debrief") {
      App.assistant = { sessionDebrief: true };
      go("assistantResults");
      return;
    }
    App.assistant = { finalText: data.final_text, proposals: (data.proposals || []).map(p => ({ ...p, include: true })), rawTranscript: data.raw_transcript };
    go("assistantReview");
  } catch (e) {
    App.error = e.message; go("assistant");
  }
}

function renderAssistantThinking() {
  el.appendChild(h(`<div class="panel processing ${ent(1)}">
    <div class="spinner"></div>
    <div class="label">Reading the vault and drafting your request...</div>
  </div>`));
}

function renderAssistantReview() {
  const a = App.assistant || {};
  el.appendChild(h(`<div class="step-title">The assistant prepared this</div>`));

  if (a.finalText) {
    el.appendChild(h(`<div class="panel ${ent(1)} asst-final"><p>${esc(a.finalText)}</p></div>`));
  }

  const proposals = a.proposals || [];
  if (!proposals.length) {
    el.appendChild(h(`<div class="panel ${ent(2)}"><div class="hint" style="margin:8px auto">No documents or drafts to file. Nothing will be saved.</div></div>`));
  } else {
    el.appendChild(h(`<div class="step-title">Approve what to keep</div>`));
    const list = h(`<div class="panel ${ent(2)}"></div>`);
    proposals.forEach((p, i) => {
      let title = "", body = "";
      if (p.type === "worksheet") {
        title = "Worksheet: " + esc(p.title || "Untitled");
        const preview = (p.markdown_body || "").split("\n").filter(Boolean).slice(0, 4).join("\n");
        body = `<pre class="asst-preview">${esc(preview)}</pre>` + (p.client_id ? `<div class="a-sub">Files under client ${esc(p.client_id)}</div>` : `<div class="a-sub">Files to the shared library</div>`);
      } else if (p.type === "email") {
        title = "Email draft to " + esc(p.client_id || "client");
        body = `<div class="a-sub">Subject: ${esc(p.subject || "")}</div><pre class="asst-preview">${esc((p.body || "").slice(0, 400))}</pre>` + (p.attach_worksheet ? `<div class="a-sub">Attaches the worksheet above</div>` : "");
      } else {
        title = esc(p.type || "item");
      }
      const row = h(`<label class="action ${p.include ? "on" : "off"}">
        <input type="checkbox" ${p.include ? "checked" : ""} data-i="${i}" />
        <div class="body"><div class="a-title">${title}</div>${body}</div>
      </label>`);
      const cb = row.querySelector("input");
      cb.onchange = () => { proposals[i].include = cb.checked; row.classList.toggle("on", cb.checked); row.classList.toggle("off", !cb.checked); };
      list.appendChild(row);
    });
    el.appendChild(list);
  }

  const anyIncludable = proposals.length > 0;
  const bar = h(`<div class="actions-bar ${ent(3)}">
    <button class="btn btn-ghost" id="asstDiscard">Discard</button>
    <div class="grow"></div>
    ${anyIncludable ? `<button class="btn btn-primary" id="asstApprove">Approve and file</button>` : `<button class="btn btn-primary" id="asstDone">Done</button>`}
  </div>`);
  bar.querySelector("#asstDiscard").onclick = () => { App.assistant = null; go("assistant"); };
  if (anyIncludable) bar.querySelector("#asstApprove").onclick = executeAssistant;
  else bar.querySelector("#asstDone").onclick = () => { App.assistant = null; go("clients"); };
  el.appendChild(bar);
}

async function executeAssistant() {
  const a = App.assistant || {};
  const proposals = (a.proposals || []).filter(p => p.include).map(({ include, ...rest }) => rest);
  if (!proposals.length) { App.assistant = null; go("clients"); return; }
  const payload = { proposals };
  if (App.client && App.client.client_id) payload.client_id = App.client.client_id;
  payload.request = a.rawTranscript || "";
  go("assistantThinking");
  try {
    const r = await fetch("/api/assistant/execute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error((t.detail && (t.detail.fix || t.detail.error || t.detail)) || ("Server error " + r.status)); }
    const data = await r.json();
    App.assistant = { ...a, results: data.results || [] };
    go("assistantResults");
  } catch (e) {
    App.error = e.message; App.state = "assistantReview"; render();
    el.insertBefore(h(`<div class="banner banner-error">${esc(e.message)}</div>`), el.firstChild);
  }
}

function renderAssistantResults() {
  const a = App.assistant || {};
  if (a.sessionDebrief) {
    el.appendChild(h(`<div class="step-title">This sounded like a session debrief</div>`));
    el.appendChild(h(`<div class="panel ${ent(1)} asst-final"><p>Use New debrief so it is filed properly as a clinical note. The assistant is for making resources, drafting emails, and looking things up.</p></div>`));
    const bar = h(`<div class="actions-bar ${ent(2)}"><div class="grow"></div><button class="btn btn-primary" id="asstToClients">Back to clients</button></div>`);
    bar.querySelector("#asstToClients").onclick = () => { App.assistant = null; go("clients"); };
    el.appendChild(bar);
    return;
  }
  el.appendChild(h(`<div class="step-title">Done</div>`));
  const panel = h(`<div class="panel ${ent(1)}"></div>`);
  (a.results || []).forEach(r => {
    const title = r.type === "worksheet" ? "Worksheet filed" : r.type === "email" ? "Email draft" : esc(r.type);
    const detail = r.status === "ok" ? (r.path || r.detail || "done") : (r.error || r.status);
    panel.appendChild(h(`<div class="result-action">
      <div class="status-dot status-${esc(r.status)}"></div>
      <div><div class="r-title">${title}</div><div class="r-detail">${esc(detail)}</div></div>
    </div>`));
  });
  if (!(a.results || []).length) panel.appendChild(h(`<div class="hint" style="margin:8px auto">Nothing was filed.</div>`));
  el.appendChild(panel);
  const bar = h(`<div class="actions-bar ${ent(2)}">
    <div class="grow"></div>
    <button class="btn btn-primary" id="asstAgain">Ask again</button>
  </div>`);
  bar.querySelector("#asstAgain").onclick = () => { App.assistant = null; go("assistant"); };
  el.appendChild(bar);
}

async function toggleRecord() {
  if (App.mediaRecorder && App.mediaRecorder.state === "recording") { stopRecord(); return; }
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    App.error = "Microphone access was blocked. Allow the mic and try again."; render(); return;
  }
  App.chunks = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
  App.mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
  App.mediaRecorder.ondataavailable = e => { if (e.data.size) App.chunks.push(e.data); };
  App.mediaRecorder.onstop = onRecordingStopped;
  App.mediaRecorder.start();
  App.seconds = 0;
  const btn = document.getElementById("recBtn");
  if (btn) { btn.classList.add("recording"); btn.setAttribute("aria-label", "Stop recording"); }
  const wrap = document.getElementById("recWrap");
  if (wrap) wrap.classList.add("recording");
  const timerEl = document.getElementById("timer");
  if (timerEl) timerEl.classList.remove("idle");
  const wave = document.getElementById("wave");
  if (wave) wave.classList.add("on");
  startMeter();
  const hint = document.getElementById("recHint");
  if (hint) hint.textContent = "Recording. Tap again when you are done.";
  App.timerId = setInterval(() => {
    App.seconds++;
    const t = document.getElementById("timer");
    if (t) t.textContent = fmtSecs(App.seconds);
  }, 1000);
}

// Live input level meter: drives the waveform bars from the real mic stream.
// Purely decorative, so every failure path is swallowed and never blocks recording.
function startMeter() {
  try {
    App.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = App.audioCtx.createMediaStreamSource(stream);
    App.analyser = App.audioCtx.createAnalyser();
    App.analyser.fftSize = 256;
    App.analyser.smoothingTimeConstant = 0.75;
    src.connect(App.analyser);
    const bars = Array.from(document.querySelectorAll("#wave i"));
    const data = new Uint8Array(App.analyser.frequencyBinCount);
    const loop = () => {
      App.analyser.getByteFrequencyData(data);
      for (let i = 0; i < bars.length; i++) {
        const v = data[2 + Math.floor(i * (data.length * 0.6) / bars.length)] / 255;
        bars[i].style.transform = `scaleY(${Math.max(0.12, Math.pow(v, 1.4)).toFixed(3)})`;
      }
      App.raf = requestAnimationFrame(loop);
    };
    loop();
  } catch (e) { /* meter is optional */ }
}
function stopMeter() {
  if (App.raf) { cancelAnimationFrame(App.raf); App.raf = null; }
  if (App.audioCtx) { App.audioCtx.close().catch(() => {}); App.audioCtx = null; }
  App.analyser = null;
}

function stopRecord() {
  if (App.timerId) { clearInterval(App.timerId); App.timerId = null; }
  stopMeter();
  if (App.mediaRecorder && App.mediaRecorder.state === "recording") App.mediaRecorder.stop();
}
function stopStream() {
  stopRecord();
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
}

async function onRecordingStopped() {
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  const blob = new Blob(App.chunks, { type: "audio/webm" });
  if (App.recMode === "assistant") { submitAssistant({ blob }); return; }
  go("processing");
  const fd = new FormData();
  fd.append("audio", blob, "debrief.webm");
  fd.append("client_id", App.client.client_id);
  try {
    const r = await fetch("/api/debrief", { method: "POST", body: fd });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Server error " + r.status)); }
    App.plan = await r.json();
    go("review");
  } catch (e) {
    App.error = e.message; go("record");
  }
}

async function executePlan() {
  const boxes = Array.from(document.querySelectorAll(".action input[type=checkbox]"));
  const plan = JSON.parse(JSON.stringify(App.plan));
  boxes.forEach(b => { const i = +b.dataset.i; if (plan.actions[i]) plan.actions[i].enabled = b.checked; });
  plan.verify = true;
  go("executing");
  try {
    const r = await fetch("/api/execute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(plan) });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Server error " + r.status)); }
    App.result = await r.json();
    go("results");
  } catch (e) {
    App.state = "review"; render();
    el.insertBefore(h(`<div class="banner banner-error">${esc(e.message)}</div>`), el.firstChild);
  }
}

// ===========================================================================
// Records UI: client record, document view, library, trash, search
// ===========================================================================

const FTYPE = {
  "session-note": { badge: "note", label: "NOTE" },
  "worksheet-pdf": { badge: "pdf", label: "PDF" },
  "upload-pdf": { badge: "pdf", label: "PDF" },
  "upload-image": { badge: "img", label: "IMG" },
  "upload-docx": { badge: "docx", label: "DOC" },
  "markdown": { badge: "note", label: "MD" },
};

function firstName(name) { return (name || "").trim().split(/\s+/)[0] || "client"; }

function fmtNextSession(iso) {
  if (!iso) return "Not scheduled";
  const d = new Date(iso);
  // Return plain text; the sole caller escapes the result (avoids double-escape).
  if (isNaN(d.getTime())) return String(iso);
  const day = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  return `${day} · ${fmtClock(d)}`;
}

async function openClient(c) {
  App.client = c;
  App.nav = "client:" + c.client_id;
  App.recordTab = "sessions";
  App.state = "clientRecord";
  App.recordData = null;
  App.error = null;
  render();
  try {
    const r = await fetch("/api/clients/" + encodeURIComponent(c.client_id));
    if (!r.ok) throw new Error("Could not load record (" + r.status + ")");
    App.recordData = await r.json();
  } catch (e) { App.error = e.message; }
  if (App.state === "clientRecord") render();
}

function renderClientRecord() {
  const d = App.recordData;
  const c = App.client || {};
  const name = (d && d.profile && d.profile.name) || c.name || "Client";
  el.appendChild(h(`<div class="crumb"><span class="link" id="crHome">Clients</span> / <b>${esc(name)}</b></div>`));
  el.querySelector("#crHome").onclick = () => { App.client = null; go("clients"); };

  if (!d) { el.appendChild(h(`<div class="panel processing"><div class="spinner"></div><div class="label">Opening record...</div></div>`)); return; }
  const pf = d.profile || {};
  const framework = pf.framework || c.framework || "";
  const concerns = (pf.presenting_concerns || []).join(", ");
  const head = h(`<div class="chead">
    <div class="bigmono">${esc(initials(name))}</div>
    <div>
      <h1>${esc(name)}</h1>
      <div class="c-meta">${esc(pf.client_id || c.client_id || "")}${framework ? " · " + esc(framework) : ""}${concerns ? " · " + esc(concerns) : ""}</div>
    </div>
    <div class="c-next">Next session<b>${esc(fmtNextSession(d.next_session))}</b></div>
  </div>`);
  el.appendChild(head);

  const tabs = h(`<div class="ctabs">
    <button class="ctab ${App.recordTab === "sessions" ? "on" : ""}" data-tab="sessions">Sessions</button>
    <button class="ctab ${App.recordTab === "profile" ? "on" : ""}" data-tab="profile">Profile</button>
    <button class="ctab ${App.recordTab === "documents" ? "on" : ""}" data-tab="documents">Documents</button>
  </div>`);
  tabs.querySelectorAll(".ctab").forEach(t => t.onclick = () => { App.recordTab = t.dataset.tab; render(); });
  el.appendChild(tabs);

  if (App.recordTab === "sessions") renderSessionsTab(d);
  else if (App.recordTab === "profile") renderProfileTab(d);
  else renderDocumentsTab(d);
}

function renderSessionsTab(d) {
  const sessions = d.sessions || [];
  const card = h(`<div class="rcard"></div>`);
  if (!sessions.length) card.appendChild(h(`<div class="hint" style="padding:24px">No session notes yet. Use New debrief to record one.</div>`));
  sessions.forEach(s => {
    const dd = (s.date || "").split("-");
    const day = dd[2] || "";
    const mon = dd[1] ? ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][+dd[1]] : "";
    const chip = s.risk_flag
      ? `<span class="rchip risk">⚑ Risk flag noted</span>`
      : `<span class="rchip ok">✓ Filed</span>`;
    const row = h(`<button class="sesscard">
      <div class="sdate"><div class="d">${esc(day)}</div><div class="m">${esc(mon)}</div></div>
      <div>
        <h4>${esc(s.title)}</h4>
        <p>${esc(s.preview || "")}</p>
        ${chip}
      </div>
    </button>`);
    row.onclick = () => openDocument(s.path, { client: d, section: "Sessions", title: s.title });
    card.appendChild(row);
  });
  el.appendChild(card);
}

function renderProfileTab(d) {
  const pf = d.profile || {};
  const grid = h(`<div style="max-width:520px"></div>`);
  const card = h(`<div class="rcard"><div class="profcard"></div></div>`);
  const rows = card.querySelector(".profcard");
  const addRow = (k, v) => { if (v) rows.appendChild(h(`<div class="profrow"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)); };
  addRow("Client id", pf.client_id);
  addRow("Status", pf.status);
  addRow("Framework", pf.framework);
  addRow("Intake date", pf.intake_date);
  addRow("Email", pf.email);
  addRow("Diagnosis", (pf.diagnosis || []).join(", "));
  addRow("Concerns", (pf.presenting_concerns || []).join(", "));
  // Treatment goals come from the summary body; show any risk flags too.
  if ((pf.risk_flags || []).length) addRow("Risk flags", pf.risk_flags.join(", "));
  grid.appendChild(card);
  if (d.summary) {
    grid.appendChild(h(`<h3 class="rsec" style="margin-top:22px">Summary</h3>`));
    grid.appendChild(h(`<div class="rcard"><div class="profcard" style="font-size:14.5px;line-height:1.6;color:var(--ink-soft)">${esc(d.summary)}</div></div>`));
  }
  el.appendChild(grid);
}

function renderDocumentsTab(d) {
  const docs = d.documents || [];
  const wrap = h(`<div class="docs" style="margin-top:0"></div>`);
  const grid = h(`<div class="docgrid"></div>`);
  docs.forEach(doc => grid.appendChild(buildDocCard(doc, d)));

  const hiddenInput = h(`<input type="file" style="display:none" accept=".pdf,.png,.jpg,.jpeg,.docx,.md" />`);
  hiddenInput.onchange = () => { if (hiddenInput.files.length) uploadFile(hiddenInput.files[0], d.client_id); };
  const add = h(`<button class="doc add">＋ Add document</button>`);
  add.onclick = () => hiddenInput.click();
  grid.appendChild(add);
  grid.appendChild(hiddenInput);

  // Drag and drop onto the grid.
  grid.addEventListener("dragover", (e) => { e.preventDefault(); grid.classList.add("drop-active"); });
  grid.addEventListener("dragleave", () => grid.classList.remove("drop-active"));
  grid.addEventListener("drop", (e) => {
    e.preventDefault(); grid.classList.remove("drop-active");
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0], d.client_id);
  });

  wrap.appendChild(grid);
  el.appendChild(wrap);
}

function buildDocCard(doc, d) {
  const ft = FTYPE[doc.kind] || FTYPE.markdown;
  const meta = (doc.agent_made ? "Created by agent" : "Uploaded") + (doc.date_display ? " · " + doc.date_display : "");
  const card = h(`<div class="doc">
    <div class="ftype ${ft.badge}">${ft.label}</div>
    <div style="flex:1;min-width:0"><h5>${esc(doc.title)}</h5><div class="d-sub">${esc(meta)}</div></div>
    <div class="acts">
      <button class="iconbtn" title="Rename">✎</button>
      <button class="iconbtn" title="Download PDF">⬇</button>
      <button class="iconbtn" title="Email draft">✉</button>
    </div>
  </div>`);
  const isMd = doc.path.toLowerCase().endsWith(".md");
  card.onclick = (e) => {
    if (e.target.closest(".acts")) return;
    if (isMd) openDocument(doc.path, { client: d, section: "Documents", title: doc.title });
    else if (doc.path.toLowerCase().endsWith(".pdf")) downloadPdf(doc.path);
    else post("/api/open", { path: doc.path });  // open images/docx in their native app
  };
  const [renameBtn, dlBtn, emailBtn] = card.querySelectorAll(".iconbtn");
  renameBtn.onclick = (e) => { e.stopPropagation(); inlineRenameCard(card, doc, d); };
  dlBtn.onclick = (e) => { e.stopPropagation(); if (isMd || doc.path.toLowerCase().endsWith(".pdf")) downloadPdf(doc.path); else post("/api/open", { path: doc.path }); };
  emailBtn.onclick = (e) => { e.stopPropagation(); emailDocument(d.client_id, doc.path); };
  return card;
}

function inlineRenameCard(card, doc, d) {
  const titleEl = card.querySelector("h5");
  const input = h(`<input class="doc-rename-input" value="${esc(doc.title)}" />`);
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  const commit = async () => {
    const v = input.value.trim();
    if (v && v !== doc.title) { await renameDocument(doc.path, v); openClient(App.client); }
    else render();
  };
  input.onkeydown = (e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") render(); };
  input.onblur = commit;
}

async function uploadFile(file, clientId) {
  const fd = new FormData();
  fd.append("client_id", clientId);
  fd.append("file", file, file.name);
  try {
    const r = await fetch("/api/documents/upload", { method: "POST", body: fd });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Upload failed " + r.status)); }
    openClient(App.client);
  } catch (e) { App.error = e.message; render(); }
}

async function renameDocument(path, newTitle) {
  try {
    const r = await fetch("/api/documents/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, new_title: newTitle }) });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Rename failed " + r.status)); }
    return (await r.json()).path;
  } catch (e) { App.error = e.message; render(); }
}

function downloadPdf(path) {
  window.open("/api/documents/pdf?path=" + encodeURIComponent(path), "_blank");
}

async function emailDocument(clientId, path, subject, body) {
  try {
    const payload = { client_id: clientId, path };
    if (subject) payload.subject = subject;
    if (body) payload.body = body;
    const r = await fetch("/api/documents/email", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Could not draft email " + r.status)); }
    toast("Mail draft opened for review. Nothing was sent.");
  } catch (e) { toast(e.message); }
}

function toast(msg) {
  const t = h(`<div class="banner" style="position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:60;background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);color:var(--ink)">${esc(msg)}</div>`);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

// ---------------------------------------------------------------------------
// Document view
// ---------------------------------------------------------------------------

async function openDocument(path, crumb) {
  App.state = "document";
  App.doc = null;
  App.docCrumb = crumb || null;
  App.error = null;
  render();
  try {
    const r = await fetch("/api/notes?path=" + encodeURIComponent(path));
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Could not open note " + r.status)); }
    App.doc = await r.json();
  } catch (e) { App.error = e.message; }
  if (App.state === "document") render();
}

function renderDocument() {
  const doc = App.doc;
  const crumb = App.docCrumb || {};
  const d = crumb.client;
  const clientName = (d && d.profile && d.profile.name) || (App.client && App.client.name) || "Client";
  const bc = h(`<div class="crumb"><span class="link" id="dcClient">${esc(clientName)}</span> / ${esc(crumb.section || "Documents")} / <b>${esc((doc && frontTitle(doc)) || crumb.title || "Document")}</b></div>`);
  el.appendChild(bc);
  bc.querySelector("#dcClient").onclick = () => { if (d) openClient(App.client); else go("clients"); };

  if (App.error) el.appendChild(h(`<div class="banner banner-error">${esc(App.error)}</div>`));
  if (!doc) { el.appendChild(h(`<div class="panel processing"><div class="spinner"></div><div class="label">Opening document...</div></div>`)); return; }

  const fm = doc.frontmatter || {};
  const isSession = doc.kind === "session-note";
  const title = frontTitle(doc);

  const bar = h(`<div class="docbar">
    <span class="doc-title" title="Click to rename">${esc(title)}</span>
    <span class="pencil">✎ rename</span>
    <span class="spacer"></span>
    <button class="rbtn" id="docEdit">✎ Edit</button>
    <button class="rbtn" id="docDownload">⬇ Download PDF</button>
    <button class="rbtn primary" id="docEmail">✉ Email draft to ${esc(firstName(clientName))}</button>
    <span class="overflow-wrap"><button class="rbtn" id="docMore">···</button></span>
  </div>`);
  el.appendChild(bar);

  const titleEl = bar.querySelector(".doc-title");
  const startRename = () => beginTitleRename(titleEl, doc);
  titleEl.onclick = startRename;
  bar.querySelector(".pencil").onclick = startRename;
  bar.querySelector("#docDownload").onclick = () => downloadPdf(doc.path);
  bar.querySelector("#docEmail").onclick = () => emailDocumentFromView(doc);
  bar.querySelector("#docEdit").onclick = () => beginEdit(doc, isSession);
  const moreWrap = bar.querySelector(".overflow-wrap");
  bar.querySelector("#docMore").onclick = (e) => { e.stopPropagation(); toggleOverflow(moreWrap, doc); };

  // The rendered page.
  const paper = h(`<div class="paper-wrap"><div class="paper"></div></div>`);
  paper.querySelector(".paper").innerHTML = doc.html;
  // Frontmatter chips (filed date, verified).
  const chips = [];
  if (isSession) {
    const df = (fm.session_date || "").toString().slice(0, 10);
    if (df) chips.push(`<span class="filedchip">✓ Filed ${esc(df)}</span>`);
    if ((fm.actions_taken || []).some(a => String(a).includes("verified"))) chips.push(`<span class="filedchip">verified on screen</span>`);
  }
  if (chips.length) paper.querySelector(".paper").appendChild(h(`<div class="paper-chips">${chips.join("")}</div>`));
  el.appendChild(paper);
}

function frontTitle(doc) {
  const fm = doc.frontmatter || {};
  if (fm.title) return String(fm.title);
  if (doc.kind === "session-note") {
    const n = fm.session_number;
    return n ? `Session ${n}, ${fm.format || "DAP"} note` : `${fm.format || "DAP"} note`;
  }
  return (App.docCrumb && App.docCrumb.title) || "Document";
}

function beginTitleRename(titleEl, doc) {
  const current = titleEl.textContent;
  const input = h(`<input class="doc-title-input" value="${esc(current)}" />`);
  titleEl.replaceWith(input);
  input.focus(); input.select();
  const commit = async () => {
    const v = input.value.trim();
    if (v && v !== current) {
      const newPath = await renameDocument(doc.path, v);
      if (newPath) { doc.path = newPath; if (App.docCrumb) App.docCrumb.title = v; }
    }
    render();
  };
  input.onkeydown = (e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") render(); };
  input.onblur = commit;
}

function beginEdit(doc, isSession) {
  const paper = el.querySelector(".paper");
  if (!paper) return;
  paper.innerHTML = "";
  if (isSession) {
    paper.appendChild(h(`<div class="amend-note">Filed notes keep their history; your change is added as a dated amendment.</div>`));
  }
  const bodyMd = stripFrontmatter(doc.markdown);
  const area = h(`<textarea class="doc-edit-area">${esc(isSession ? "" : bodyMd)}</textarea>`);
  if (isSession) area.placeholder = "Write an amendment. It will be appended with today's date.";
  paper.appendChild(area);
  const row = h(`<div class="actions-bar" style="margin-top:14px">
    <button class="rbtn" id="editCancel">Cancel</button><div class="grow" style="flex:1"></div>
    <button class="rbtn primary" id="editSave">${isSession ? "Add amendment" : "Save"}</button>
  </div>`);
  paper.appendChild(row);
  area.focus();
  row.querySelector("#editCancel").onclick = () => render();
  row.querySelector("#editSave").onclick = async () => {
    const text = area.value.trim();
    if (!text) { render(); return; }
    if (isSession) await amendNote(doc.path, text);
    else await saveDocument(doc.path, text);
    openDocument(doc.path, App.docCrumb);
  };
}

function stripFrontmatter(md) {
  if (md && md.startsWith("---")) {
    const end = md.indexOf("\n---", 3);
    if (end !== -1) return md.slice(md.indexOf("\n", end + 1) + 1).replace(/^\n+/, "");
  }
  return md || "";
}

async function amendNote(path, text) {
  try {
    const r = await fetch("/api/notes/amend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, text }) });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Amend failed " + r.status)); }
  } catch (e) { toast(e.message); }
}

async function saveDocument(path, markdown) {
  try {
    const r = await fetch("/api/documents/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, markdown }) });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Save failed " + r.status)); }
  } catch (e) { toast(e.message); }
}

function emailDocumentFromView(doc) {
  const cid = (App.client && App.client.client_id) || (App.docCrumb && App.docCrumb.client && App.docCrumb.client.client_id);
  if (cid) emailDocument(cid, doc.path);
  else toast("Open this document from a client record to email it.");
}

function toggleOverflow(wrap, doc) {
  const existing = wrap.querySelector(".overflow-menu");
  if (existing) { existing.remove(); return; }
  const menu = h(`<div class="overflow-menu">
    <button data-a="reveal">Reveal in Finder</button>
    <button data-a="open">Open</button>
    <button class="danger" data-a="trash">Move to Trash</button>
  </div>`);
  wrap.appendChild(menu);
  menu.querySelector('[data-a="reveal"]').onclick = () => { post("/api/reveal", { path: doc.path }); menu.remove(); };
  menu.querySelector('[data-a="open"]').onclick = () => { post("/api/open", { path: doc.path }); menu.remove(); };
  menu.querySelector('[data-a="trash"]').onclick = async () => {
    menu.remove();
    await post("/api/trash", { path: doc.path });
    toast("Moved to Trash. Restore it from the Trash view.");
    if (App.client) openClient(App.client); else go("clients");
  };
  const closer = (e) => { if (!wrap.contains(e.target)) { menu.remove(); document.removeEventListener("click", closer); } };
  setTimeout(() => document.addEventListener("click", closer), 0);
}

async function post(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Error " + r.status)); }
    return await r.json();
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------------------
// Library
// ---------------------------------------------------------------------------

const LIB_THUMBS = { breathing: "🫁", thought: "📝", sleep: "🌙", values: "🧭", grounding: "⚓", record: "📝" };
function libThumb(title) {
  const t = (title || "").toLowerCase();
  for (const k in LIB_THUMBS) if (t.includes(k)) return LIB_THUMBS[k];
  return "📄";
}

async function openLibrary(which) {
  App.nav = "lib:" + which;
  App.state = "library";
  App.library = App.library || null;
  App.libWhich = which;
  App.error = null;
  render();
  try {
    const r = await fetch("/api/library");
    if (!r.ok) throw new Error("Could not load library (" + r.status + ")");
    App.library = await r.json();
  } catch (e) { App.error = e.message; }
  if (App.state === "library") render();
}

function renderLibrary() {
  const which = App.libWhich || "worksheets";
  const lib = App.library;
  el.appendChild(h(`<div class="lib-head">
    <div class="grow">
      <h1>${which === "reference" ? "Reference library" : "Worksheet library"}</h1>
      <div class="c-meta">${assistantEnabled() ? "Reusable client resources. Ask the assistant for a new one by voice, or add your own." : "Reusable client resources. Add your own from a client's Documents tab."}</div>
    </div>
    ${assistantEnabled() ? '<button class="rbtn primary" id="libAsk">🎙 Ask for a worksheet</button>' : ""}
  </div>`));
  const libAsk = el.querySelector("#libAsk");
  if (libAsk) libAsk.onclick = () => { App.assistant = null; go("assistant"); };
  if (App.error) el.appendChild(h(`<div class="banner banner-error">${esc(App.error)}</div>`));
  if (!lib) { el.appendChild(h(`<div class="panel processing"><div class="spinner"></div><div class="label">Loading library...</div></div>`)); return; }

  const items = which === "reference" ? (lib.reference || []) : (lib.worksheets || []);
  const grid = h(`<div class="libgrid"></div>`);
  if (!items.length) grid.appendChild(h(`<div class="hint">Nothing here yet.</div>`));
  items.forEach(it => {
    const isAgent = it.agent_made;
    const card = h(`<div class="libcard">
      <div class="thumb">${libThumb(it.title)}</div>
      <h5>${esc(it.title)}</h5>
      <p>${esc(it.kind === "worksheet-pdf" || it.kind === "markdown" ? "Reusable client resource." : "")}</p>
      <div class="lib-row">
        <span class="rchip ${isAgent ? "agent" : "ok"}">${isAgent ? "✦ Agent-made" : "Template"}</span>
        <span class="lib-send">Email to client…</span>
      </div>
    </div>`);
    card.querySelector(".lib-send").onclick = (e) => { e.stopPropagation(); pickClientThen(cid => emailDocument(cid, it.path)); };
    card.onclick = () => openDocument(it.path, { section: which === "reference" ? "Reference" : "Worksheets", title: it.title });
    grid.appendChild(card);
  });
  el.appendChild(grid);
}

function pickClientThen(cb) {
  const backdrop = h(`<div class="modal-backdrop"><div class="modal"><h4>Email to which client?</h4><div class="picks"></div><div style="text-align:right;margin-top:8px"><button class="rbtn" id="pickCancel">Cancel</button></div></div></div>`);
  const picks = backdrop.querySelector(".picks");
  App.clients.forEach(c => {
    const b = h(`<button class="pick">${esc(c.name)} <span style="color:var(--ink-faint)">${esc(c.client_id)}</span></button>`);
    b.onclick = () => { backdrop.remove(); cb(c.client_id); };
    picks.appendChild(b);
  });
  backdrop.querySelector("#pickCancel").onclick = () => backdrop.remove();
  backdrop.onclick = (e) => { if (e.target === backdrop) backdrop.remove(); };
  document.body.appendChild(backdrop);
}

// ---------------------------------------------------------------------------
// Trash
// ---------------------------------------------------------------------------

async function openTrash() {
  App.nav = "trash";
  App.state = "trash";
  App.trash = null;
  App.error = null;
  render();
  try {
    const r = await fetch("/api/trash");
    if (!r.ok) throw new Error("Could not load trash (" + r.status + ")");
    App.trash = (await r.json()).items || [];
  } catch (e) { App.error = e.message; }
  if (App.state === "trash") render();
}

function renderTrash() {
  el.appendChild(h(`<h1 style="font-family:var(--font-serif);font-size:26px;font-weight:600;margin-bottom:16px">Trash</h1>`));
  if (App.error) el.appendChild(h(`<div class="banner banner-error">${esc(App.error)}</div>`));
  if (App.trash === null) { el.appendChild(h(`<div class="panel processing"><div class="spinner"></div><div class="label">Loading...</div></div>`)); return; }
  const list = h(`<div class="trash-list"></div>`);
  if (!App.trash.length) list.appendChild(h(`<div class="hint">Trash is empty.</div>`));
  App.trash.forEach(item => {
    const row = h(`<div class="trash-row">
      <div class="t-body"><div class="t-title">${esc(item.title)}</div><div class="t-when">${esc(item.original)} · ${esc((item.trashed_at || "").slice(0, 10))}</div></div>
      <button class="rbtn">Restore</button>
    </div>`);
    row.querySelector("button").onclick = async () => { await post("/api/trash/restore", { token: item.token }); openTrash(); };
    list.appendChild(row);
  });
  el.appendChild(list);
  el.appendChild(h(`<div class="trash-note">Items are removed after 30 days.</div>`));
}

// ---------------------------------------------------------------------------
// Global search
// ---------------------------------------------------------------------------

function runSearch(q) {
  clearTimeout(App.searchTimer);
  const box = document.getElementById("searchResults");
  if (!box) return;
  const query = (q || "").trim();
  if (!query) { box.innerHTML = ""; box.className = ""; return; }
  App.searchTimer = setTimeout(async () => {
    try {
      const r = await fetch("/api/search?q=" + encodeURIComponent(query));
      if (!r.ok) return;
      const data = await r.json();
      renderSearchResults(box, data, query);
    } catch (e) { /* ignore */ }
  }, 220);
}

function renderSearchResults(box, data, query) {
  box.innerHTML = "";
  box.className = "search-results";
  const groups = [
    ["Clients", data.clients || []],
    ["Notes", data.notes || []],
    ["Library", data.library || []],
  ];
  let any = false;
  groups.forEach(([label, hits]) => {
    if (!hits.length) return;
    any = true;
    box.appendChild(h(`<div class="search-group-label">${esc(label)}</div>`));
    hits.forEach(hit => {
      const item = h(`<button class="search-hit"><div class="sh-title">${esc(hit.title)}</div><div class="sh-snip">${esc(hit.snippet || "")}</div></button>`);
      item.onclick = () => { closeSearch(); openSearchHit(hit); };
      box.appendChild(item);
    });
  });
  if (!any) box.appendChild(h(`<div class="search-empty">No matches for "${esc(query)}".</div>`));
}

function closeSearch() {
  const s = document.getElementById("globalSearch");
  const box = document.getElementById("searchResults");
  if (s) s.value = "";
  if (box) { box.innerHTML = ""; box.className = ""; }
}

function openSearchHit(hit) {
  const p = hit.path || "";
  const m = p.match(/^Clients\/([^/]+)\/_Profile\.md$/);
  if (m) {
    const c = App.clients.find(x => x.client_id === m[1]);
    if (c) { openClient(c); return; }
  }
  const cm = p.match(/^Clients\/([^/]+)\//);
  const crumb = { section: "Sessions", title: hit.title };
  if (cm) {
    const c = App.clients.find(x => x.client_id === cm[1]);
    if (c) App.client = c;
  }
  openDocument(p, crumb);
}

// ===========================================================================
// Settings screen: profession, format, template import, dictionary, engine,
// features. Every change POSTs a small patch to /api/settings and refreshes.
// ===========================================================================

const STT_ENGINES = [
  { id: "parakeet", label: "Parakeet", desc: "Fastest, excellent English" },
  { id: "mlx-whisper", label: "MLX Whisper", desc: "Most accurate for other languages and accents" },
];

const FEATURE_ROWS = [
  { key: "calendar", label: "Calendar booking", desc: "Book follow-up appointments in a dedicated Debrief calendar." },
  { key: "email", label: "Email drafts", desc: "Prepare client emails for your review. Debrief never sends them." },
  { key: "verify", label: "On-screen verification", desc: "Read the screen after running to confirm what happened." },
  { key: "assistant", label: "Assistant", desc: "Ask for worksheets, email drafts, and quick lookups." },
];

const STT_DOWNLOAD_HINT = "First transcription after switching may take a minute while the model downloads.";

// Turn an /api/settings error body into a readable line.
function apiErr(data, status) {
  const d = data && data.detail;
  if (d) return (d.fix || d.error || (typeof d === "string" ? d : null)) || ("Error " + status);
  if (data && data.error) return data.error;
  return "Error " + status;
}

async function postSettings(patch, dictionary) {
  const body = {};
  if (patch) body.settings = patch;
  if (dictionary !== undefined) body.dictionary = dictionary;
  const r = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(apiErr(data, r.status));
  App.settingsPayload = data;
  App.settings = data.settings || SETTINGS_DEFAULTS;
  return data;
}

// The quiet save confirmation. It fades in with a small rise, settles for about
// a second and a half, then fades out; the word is only cleared once that fade
// has finished, so it never blinks out mid-transition.
function settingsSaved(node) {
  if (!node) return;
  node.textContent = "Saved";
  clearTimeout(node._t);
  clearTimeout(node._tClear);
  // Next frame, so the class change animates from the reset state.
  requestAnimationFrame(() => node.classList.add("on"));
  node._t = setTimeout(() => {
    node.classList.remove("on");
    node._tClear = setTimeout(() => { if (!node.classList.contains("on")) node.textContent = ""; }, 260);
  }, 1500);
}

async function openSettings() {
  App.state = "settings";
  App.nav = "settings";
  App.error = null;
  render();
  await refreshSettings();
  if (App.state === "settings") render();
}

// Re-render the whole shell (used when a settings change alters the sidebar,
// e.g. turning the assistant off hides its entry).
function rebuildShell() {
  const shell = document.getElementById("shell");
  if (shell) shell.innerHTML = "";
  render();
}

function renderSettings() {
  const payload = App.settingsPayload || settingsFallbackPayload();
  const s = payload.settings || SETTINGS_DEFAULTS;
  const professions = payload.professions || PROFESSIONS_FALLBACK;
  const formats = payload.formats || FORMATS_FALLBACK;
  const feats = Object.assign({ calendar: true, email: true, verify: true, assistant: true }, s.features || {});
  const dictText = payload.dictionary || "";

  el.appendChild(h(`<div class="setup-head ${ent(1)}"><h1>Settings</h1><p class="setup-lead">Tune Debrief for your work. Everything here is stored on this Mac.</p></div>`));

  // ---- Card A: profession and note format ----
  const cardA = h(`<div class="panel setup-card ${ent(2)}">
    <div class="setup-step-head"><h2>Profession and note format</h2><span class="set-saved" id="savedA"></span></div>
    <p class="setup-note">This shapes the vocabulary Debrief expects and the note structure it writes.</p>
  </div>`);
  const profField = h(`<div class="set-field"><label class="set-label">Profession</label></div>`);
  const profSel = h(`<select class="set-select" id="setProf"></select>`);
  professions.forEach(pf => {
    const o = h(`<option value="${esc(pf.id)}">${esc(pf.name)}</option>`);
    if (pf.id === s.profession) o.selected = true;
    profSel.appendChild(o);
  });
  profSel.onchange = async () => {
    try { await postSettings({ profession: profSel.value }); settingsSaved(cardA.querySelector("#savedA")); }
    catch (e) { toast(e.message); }
  };
  profField.appendChild(profSel);
  cardA.appendChild(profField);

  const fmtField = h(`<div class="set-field"><label class="set-label">Active note format</label></div>`);
  const fmtSel = h(`<select class="set-select" id="setFmt"></select>`);
  formats.forEach(f => {
    const o = h(`<option value="${esc(f.id)}">${esc(f.name)}</option>`);
    if (f.id === s.note_format) o.selected = true;
    fmtSel.appendChild(o);
  });
  fmtSel.onchange = async () => {
    try { await postSettings({ note_format: fmtSel.value }); settingsSaved(cardA.querySelector("#savedA")); }
    catch (e) { toast(e.message); }
  };
  fmtField.appendChild(fmtSel);
  cardA.appendChild(fmtField);

  const importBtn = h(`<button class="btn btn-ghost set-import-btn" id="setImport">Import a note template</button>`);
  importBtn.onclick = () => launchImportFlow({
    profession: s.profession,
    fromWizard: false,
    onSaved: () => { openSettings(); },
  });
  cardA.appendChild(importBtn);
  el.appendChild(cardA);

  // ---- Card B: speech to text ----
  const cardB = h(`<div class="panel setup-card ${ent(3)}">
    <div class="setup-step-head"><h2>Speech to text</h2><span class="set-saved" id="savedB"></span></div>
    <p class="setup-note">Choose the transcription model that fits your voice and language.</p>
  </div>`);
  const engBox = h(`<div class="set-radios"></div>`);
  STT_ENGINES.forEach(eng => {
    const row = h(`<label class="set-radio">
      <input type="radio" name="setStt" value="${esc(eng.id)}" ${eng.id === s.stt_engine ? "checked" : ""} />
      <span class="set-radio-body"><span class="set-radio-title">${esc(eng.label)}</span><span class="set-radio-desc">${esc(eng.desc)}</span></span>
    </label>`);
    row.querySelector("input").onchange = async () => {
      try { await postSettings({ stt_engine: eng.id }); settingsSaved(cardB.querySelector("#savedB")); }
      catch (e) { toast(e.message); }
    };
    engBox.appendChild(row);
  });
  cardB.appendChild(engBox);
  cardB.appendChild(h(`<p class="setup-sub">${esc(STT_DOWNLOAD_HINT)}</p>`));
  el.appendChild(cardB);

  // ---- Card C: personal dictionary ----
  const cardC = h(`<div class="panel setup-card ${ent(4)}">
    <div class="setup-step-head"><h2>Personal dictionary</h2><span class="set-saved" id="savedC"></span></div>
    <p class="setup-note">One term or correction per line. These teach the transcriber your names and jargon.</p>
  </div>`);
  const area = h(`<textarea class="set-textarea" id="setDict" rows="6" spellcheck="false"></textarea>`);
  area.value = dictText;
  const dictSave = h(`<div class="set-field-actions"><button class="btn btn-primary btn-compact" id="setDictSave">Save dictionary</button></div>`);
  dictSave.querySelector("#setDictSave").onclick = async () => {
    try { await postSettings(null, area.value); settingsSaved(cardC.querySelector("#savedC")); }
    catch (e) { toast(e.message); }
  };
  cardC.appendChild(area);
  cardC.appendChild(dictSave);
  el.appendChild(cardC);

  // ---- Card D: features ----
  const cardD = h(`<div class="panel setup-card ${ent(5)}">
    <div class="setup-step-head"><h2>What Debrief can do</h2><span class="set-saved" id="savedD"></span></div>
    <p class="setup-note">Turn off anything you do not use. Off features are hidden and never run.</p>
  </div>`);
  const togBox = h(`<div class="set-toggles"></div>`);
  FEATURE_ROWS.forEach(fr => {
    const on = feats[fr.key] !== false;
    const row = h(`<label class="set-toggle">
      <span class="set-toggle-body"><span class="set-toggle-title">${esc(fr.label)}</span><span class="set-toggle-desc">${esc(fr.desc)}</span></span>
      <input type="checkbox" ${on ? "checked" : ""} />
    </label>`);
    const cb = row.querySelector("input");
    cb.onchange = async () => {
      try {
        // Partial per-key patch: the server deep-merges, so we never send a
        // stale snapshot of the other toggles.
        await postSettings({ features: { [fr.key]: cb.checked } });
        feats[fr.key] = cb.checked;
        settingsSaved(cardD.querySelector("#savedD"));
        if (fr.key === "assistant") { rebuildShell(); }
      } catch (e) { cb.checked = !cb.checked; toast(e.message); }
    };
    togBox.appendChild(row);
  });
  cardD.appendChild(togBox);
  el.appendChild(cardD);
}

// ===========================================================================
// Shared template-import subflow (used from Settings and the wizard).
// A staged modal: source -> mode -> derived spec -> preview -> save. The Gemini
// API key lives only in this closure and is dropped the moment a compile call
// returns. Every dynamic string is escaped; the dry-run preview is built as DOM
// with esc(), never innerHTML.
// ===========================================================================

// Busy buttons in the import subflow show the same spinner the processing
// screens use, at label size. Static markup, never user text.
const SPINNER = '<span class="btn-spinner" aria-hidden="true"></span>';

function launchImportFlow({ profession, fromWizard, onSaved }) {
  const flow = {
    stage: "source",
    profession: profession || (App.settings && App.settings.profession) || "therapy",
    fromWizard: !!fromWizard,
    docText: "", chars: 0, truncated: false,
    mode: "local", consent: false, apiKey: "",
    spec: null, promptLayer: "", offerLocalFallback: false,
    preview: null, makeActive: !!fromWizard,
    saved: null, busy: false, error: null,
  };

  const backdrop = h(`<div class="modal-backdrop import-backdrop"><div class="modal import-modal"><div class="import-body"></div></div></div>`);
  const bodyEl = backdrop.querySelector(".import-body");
  backdrop.onclick = (e) => { if (e.target === backdrop) close(); };
  function close() { flow.apiKey = ""; backdrop.remove(); }
  function repaint() {
    bodyEl.innerHTML = "";
    renderImportStage(flow, bodyEl, { repaint, close, onSaved });
    // Re-trigger the stage transition: the container survives the repaint, so
    // the animation has to be removed and reflowed to play again.
    bodyEl.classList.remove("stage-in");
    void bodyEl.offsetWidth;
    bodyEl.classList.add("stage-in");
  }
  document.body.appendChild(backdrop);
  repaint();
}

function importHeader(flow, close, title) {
  const head = h(`<div class="import-head"><h3>${esc(title)}</h3><button class="import-x" aria-label="Close">✕</button></div>`);
  head.querySelector(".import-x").onclick = close;
  return head;
}

function renderImportStage(flow, body, ctx) {
  if (flow.stage === "source") return importStageSource(flow, body, ctx);
  if (flow.stage === "mode") return importStageMode(flow, body, ctx);
  if (flow.stage === "spec") return importStageSpec(flow, body, ctx);
  if (flow.stage === "preview") return importStagePreview(flow, body, ctx);
  if (flow.stage === "saved") return importStageSaved(flow, body, ctx);
}

function importStageSource(flow, body, ctx) {
  body.appendChild(importHeader(flow, ctx.close, "Import a note template"));
  body.appendChild(h(`<p class="import-lead">Upload a blank or example template, or paste its text. Debrief reads only the structure to build a matching note format.</p>`));
  if (flow.error) body.appendChild(h(`<div class="import-error">${esc(flow.error)}</div>`));

  const hidden = h(`<input type="file" style="display:none" accept=".md,.txt,.docx,.pdf" />`);
  hidden.onchange = () => { if (hidden.files.length) doUpload(flow, hidden.files[0], ctx); };
  const pick = h(`<button class="btn btn-ghost import-file-btn">${flow.busy ? SPINNER + "Reading..." : "Choose a file (.md, .txt, .docx)"}</button>`);
  pick.disabled = flow.busy;
  pick.onclick = () => hidden.click();
  body.appendChild(pick);
  body.appendChild(hidden);

  body.appendChild(h(`<div class="import-or">or paste the template text</div>`));
  const area = h(`<textarea class="set-textarea" rows="6" placeholder="Paste your template here"></textarea>`);
  area.value = flow.docText || "";
  body.appendChild(area);

  const bar = h(`<div class="import-bar"></div>`);
  const next = h(`<button class="btn btn-primary" ${flow.busy ? "disabled" : ""}>Continue</button>`);
  next.onclick = () => {
    const text = (area.value || "").trim();
    if (!text) { flow.error = "Add a file or paste some template text first."; ctx.repaint(); return; }
    flow.docText = text; flow.chars = text.length; flow.truncated = false; flow.error = null;
    flow.stage = "mode"; ctx.repaint();
  };
  bar.appendChild(next);
  body.appendChild(bar);
}

async function doUpload(flow, file, ctx) {
  flow.busy = true; flow.error = null; ctx.repaint();
  const fd = new FormData();
  fd.append("file", file, file.name);
  try {
    const r = await fetch("/api/settings/import/upload", { method: "POST", body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiErr(data, r.status));
    if (data.pdf_unsupported) {
      flow.error = "PDF import is not supported yet. Export the template as .docx or .md, or paste its text below.";
      flow.busy = false; ctx.repaint(); return;
    }
    flow.docText = data.doc_text || "";
    flow.chars = data.chars || 0;
    flow.truncated = !!data.truncated;
    flow.busy = false;
    flow.stage = "mode"; ctx.repaint();
  } catch (e) {
    flow.error = e.message; flow.busy = false; ctx.repaint();
  }
}

const GEMINI_CONSENT_COPY = "This sends this one document, and nothing else, ever, to Google Gemini. Use a blank or example template, not real client information.";

function importStageMode(flow, body, ctx) {
  body.appendChild(importHeader(flow, ctx.close, "How should Debrief read it?"));
  const meta = flow.chars ? `${flow.chars.toLocaleString()} characters read.` : "";
  const trunc = flow.truncated ? " The template was long, so only the first part was used." : "";
  if (meta) body.appendChild(h(`<p class="import-lead">${esc(meta + trunc)}</p>`));
  if (flow.error) body.appendChild(h(`<div class="import-error">${esc(flow.error)}</div>`));

  const radios = h(`<div class="set-radios"></div>`);
  const local = h(`<label class="set-radio">
    <input type="radio" name="impMode" value="local" ${flow.mode === "local" ? "checked" : ""} />
    <span class="set-radio-body"><span class="set-radio-title">Local</span><span class="set-radio-desc">Runs entirely on this Mac.</span></span>
  </label>`);
  const cloud = h(`<label class="set-radio">
    <input type="radio" name="impMode" value="cloud" ${flow.mode === "cloud" ? "checked" : ""} />
    <span class="set-radio-body"><span class="set-radio-title">Cloud boost</span><span class="set-radio-desc">Uses Google Gemini for one call. Bring your own API key.</span></span>
  </label>`);
  local.querySelector("input").onchange = () => { flow.mode = "local"; flow.offerLocalFallback = false; ctx.repaint(); };
  cloud.querySelector("input").onchange = () => { flow.mode = "cloud"; ctx.repaint(); };
  radios.appendChild(local);
  radios.appendChild(cloud);
  body.appendChild(radios);

  if (flow.mode === "cloud") {
    const box = h(`<div class="import-consent"></div>`);
    box.appendChild(h(`<p class="import-consent-copy">${esc(GEMINI_CONSENT_COPY)}</p>`));
    const consentRow = h(`<label class="set-toggle import-consent-check">
      <span class="set-toggle-body"><span class="set-toggle-title">I understand and consent</span></span>
      <input type="checkbox" ${flow.consent ? "checked" : ""} />
    </label>`);
    consentRow.querySelector("input").onchange = (e) => { flow.consent = e.target.checked; };
    box.appendChild(consentRow);
    const keyField = h(`<div class="set-field"><label class="set-label">Gemini API key</label></div>`);
    const key = h(`<input type="password" class="set-select" autocomplete="off" placeholder="Pasted here, kept in memory, cleared after the call" />`);
    key.value = flow.apiKey || "";
    key.oninput = () => { flow.apiKey = key.value; };
    keyField.appendChild(key);
    box.appendChild(keyField);
    body.appendChild(box);
  }

  const bar = h(`<div class="import-bar"></div>`);
  const back = h(`<button class="btn btn-ghost">Back</button>`);
  back.onclick = () => { flow.stage = "source"; flow.error = null; ctx.repaint(); };
  bar.appendChild(back);
  bar.appendChild(h(`<div class="grow"></div>`));
  if (flow.offerLocalFallback) {
    const tryLocal = h(`<button class="btn btn-ghost">Try again locally</button>`);
    tryLocal.onclick = () => { flow.mode = "local"; flow.offerLocalFallback = false; doCompile(flow, ctx); };
    bar.appendChild(tryLocal);
  }
  const compile = h(`<button class="btn btn-primary" ${flow.busy ? "disabled" : ""}>${flow.busy ? SPINNER + "Compiling..." : "Compile"}</button>`);
  compile.onclick = () => doCompile(flow, ctx);
  bar.appendChild(compile);
  body.appendChild(bar);
}

async function doCompile(flow, ctx) {
  if (flow.mode === "cloud" && (!flow.consent || !(flow.apiKey || "").trim())) {
    flow.error = "Check the consent box and paste an API key, or switch to Local.";
    ctx.repaint(); return;
  }
  flow.busy = true; flow.error = null; flow.offerLocalFallback = false; ctx.repaint();
  const payload = { doc_text: flow.docText, profession: flow.profession, mode: flow.mode };
  if (flow.mode === "cloud") { if (flow.consent) payload.consent = true; payload.api_key = (flow.apiKey || "").trim(); }
  try {
    const r = await fetch("/api/settings/import/compile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await r.json().catch(() => ({}));
    // Key is single-use: drop it the instant the call returns, whatever happened.
    flow.apiKey = "";
    if (r.status === 502 && data.fallback === "local") {
      flow.error = (data.error || "The cloud service could not compile this.") + " You can try again locally.";
      flow.offerLocalFallback = true; flow.busy = false; ctx.repaint(); return;
    }
    if (!r.ok) throw new Error(apiErr(data, r.status));
    flow.spec = data.spec;
    flow.promptLayer = data.prompt_layer || "";
    if (!flow.spec.sections) flow.spec.sections = [];
    flow.busy = false; flow.stage = "spec"; ctx.repaint();
  } catch (e) {
    flow.apiKey = ""; flow.error = e.message; flow.busy = false; ctx.repaint();
  }
}

function importStageSpec(flow, body, ctx) {
  body.appendChild(importHeader(flow, ctx.close, "Review the derived format"));
  body.appendChild(h(`<p class="import-lead">Edit the name and sections. This is the structure every note in this format will follow.</p>`));
  if (flow.error) body.appendChild(h(`<div class="import-error">${esc(flow.error)}</div>`));

  const nameField = h(`<div class="set-field"><label class="set-label">Format name</label></div>`);
  const nameInput = h(`<input type="text" class="set-select" />`);
  nameInput.value = flow.spec.name || "";
  nameInput.oninput = () => { flow.spec.name = nameInput.value; };
  nameField.appendChild(nameInput);
  body.appendChild(nameField);

  const list = h(`<div class="import-sections"></div>`);
  const renderRows = () => {
    list.innerHTML = "";
    flow.spec.sections.forEach((sec, i) => {
      const row = h(`<div class="import-sec-row">
        <div class="import-sec-fields">
          <input type="text" class="set-select import-sec-heading" placeholder="Heading" />
          <input type="text" class="set-select import-sec-desc" placeholder="What goes in this section" />
        </div>
        <button class="import-sec-del" aria-label="Remove section" ${flow.spec.sections.length <= 1 ? "disabled" : ""}>✕</button>
      </div>`);
      const heading = row.querySelector(".import-sec-heading");
      const desc = row.querySelector(".import-sec-desc");
      heading.value = sec.heading || "";
      desc.value = sec.description || "";
      heading.oninput = () => { sec.heading = heading.value; };
      desc.oninput = () => { sec.description = desc.value; };
      row.querySelector(".import-sec-del").onclick = () => {
        if (flow.spec.sections.length <= 1) return;
        flow.spec.sections.splice(i, 1); renderRows();
      };
      list.appendChild(row);
    });
  };
  renderRows();
  body.appendChild(list);

  const addRow = h(`<button class="btn btn-ghost btn-compact import-add">＋ Add section</button>`);
  addRow.onclick = () => {
    if (flow.spec.sections.length >= 12) { flow.error = "A format can have at most 12 sections."; ctx.repaint(); return; }
    flow.spec.sections.push({ heading: "", description: "" }); renderRows();
  };
  body.appendChild(addRow);

  if (!flow.fromWizard) {
    const activeRow = h(`<label class="set-toggle import-active-check">
      <span class="set-toggle-body"><span class="set-toggle-title">Make this my active format</span></span>
      <input type="checkbox" ${flow.makeActive ? "checked" : ""} />
    </label>`);
    activeRow.querySelector("input").onchange = (e) => { flow.makeActive = e.target.checked; };
    body.appendChild(activeRow);
  }

  const bar = h(`<div class="import-bar"></div>`);
  const back = h(`<button class="btn btn-ghost">Back</button>`);
  back.onclick = () => { flow.stage = "mode"; flow.error = null; ctx.repaint(); };
  bar.appendChild(back);
  bar.appendChild(h(`<div class="grow"></div>`));
  const preview = h(`<button class="btn btn-ghost" ${flow.busy ? "disabled" : ""}>${flow.busy && flow._act === "preview" ? SPINNER + "Rendering..." : "Preview"}</button>`);
  preview.onclick = () => doPreview(flow, ctx);
  bar.appendChild(preview);
  const save = h(`<button class="btn btn-primary" ${flow.busy ? "disabled" : ""}>${flow.busy && flow._act === "save" ? SPINNER + "Saving..." : "Save format"}</button>`);
  save.onclick = () => doSave(flow, ctx);
  bar.appendChild(save);
  body.appendChild(bar);
}

function specForSubmit(flow) {
  return {
    name: flow.spec.name || "",
    clinical: !!flow.spec.clinical,
    style_rules: flow.spec.style_rules || "",
    risk_section: !!flow.spec.risk_section,
    sections: (flow.spec.sections || []).map(s => ({ heading: s.heading || "", description: s.description || "" })),
  };
}

async function doPreview(flow, ctx) {
  flow.busy = true; flow._act = "preview"; flow.error = null; ctx.repaint();
  try {
    const r = await fetch("/api/settings/import/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ spec: specForSubmit(flow), profession: flow.profession }) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiErr(data, r.status));
    flow.preview = data; flow.busy = false; flow._act = null; flow.stage = "preview"; ctx.repaint();
  } catch (e) {
    flow.busy = false; flow._act = null; flow.error = e.message; ctx.repaint();
  }
}

function importStagePreview(flow, body, ctx) {
  body.appendChild(importHeader(flow, ctx.close, "Preview on a sample transcript"));
  body.appendChild(h(`<p class="import-lead">A dry run on a bundled sample. Nothing is saved. This is how a filed note will read.</p>`));
  const pv = flow.preview || {};
  const note = pv.note || {};
  const sections = pv.sections || [];
  const paper = h(`<div class="panel doc import-preview-doc"></div>`);
  paper.appendChild(h(`<div class="letterhead"><span class="who">Sample</span><span class="stamps"><span class="stamp">${esc(flow.spec.name || "Format")}</span></span></div>`));
  paper.appendChild(h(`<div class="dblrule"></div>`));
  const secBox = h(`<div class="note-sections"></div>`);
  sections.forEach(s => {
    const secEl = h(`<div class="note-section"><h4></h4><p></p></div>`);
    secEl.querySelector("h4").textContent = s.heading || "";
    secEl.querySelector("p").textContent = (note && note[s.key] != null) ? String(note[s.key]) : "";
    secBox.appendChild(secEl);
  });
  paper.appendChild(secBox);
  if (note.risk_present && note.risk) {
    const riskBox = h(`<div class="risk"><h4>Risk</h4></div>`);
    [["Ideation", "ideation"], ["Plan, intent, means", "plan_intent_means"], ["Protective factors", "protective_factors"], ["Interventions taken", "interventions_taken"]].forEach(([label, key]) => {
      const v = note.risk[key];
      if (v) { const row = h(`<div class="row"><b>${esc(label)}:</b> <span></span></div>`); row.querySelector("span").textContent = String(v); riskBox.appendChild(row); }
    });
    paper.appendChild(riskBox);
  }
  body.appendChild(paper);

  const bar = h(`<div class="import-bar"></div>`);
  const back = h(`<button class="btn btn-ghost">Back to editing</button>`);
  back.onclick = () => { flow.stage = "spec"; ctx.repaint(); };
  bar.appendChild(back);
  bar.appendChild(h(`<div class="grow"></div>`));
  const save = h(`<button class="btn btn-primary" ${flow.busy ? "disabled" : ""}>${flow.busy ? SPINNER + "Saving..." : "Save format"}</button>`);
  save.onclick = () => doSave(flow, ctx);
  bar.appendChild(save);
  body.appendChild(bar);
}

async function doSave(flow, ctx) {
  flow.busy = true; flow._act = "save"; flow.error = null; ctx.repaint();
  const payload = { spec: specForSubmit(flow), set_active: flow.fromWizard ? true : !!flow.makeActive };
  if (flow.promptLayer) payload.prompt_layer = flow.promptLayer;
  try {
    const r = await fetch("/api/settings/import/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiErr(data, r.status));
    flow.saved = data; flow.busy = false; flow._act = null; flow.stage = "saved";
    await refreshSettings();
    ctx.repaint();
    if (ctx.onSaved) ctx.onSaved(data);
  } catch (e) {
    flow.busy = false; flow._act = null; flow.error = e.message;
    if (flow.stage === "preview") flow.stage = "spec";
    ctx.repaint();
  }
}

function importStageSaved(flow, body, ctx) {
  body.appendChild(importHeader(flow, ctx.close, "Format saved"));
  const saved = flow.saved || {};
  body.appendChild(h(`<div class="import-saved">
    <div class="import-saved-check">✓</div>
    <div><div class="import-saved-name">${esc(saved.name || "Your format")}</div>
    <div class="import-saved-meta">${esc((saved.sections || 0) + " sections")}${saved.active ? " · now your active format" : ""}</div></div>
  </div>`));
  const bar = h(`<div class="import-bar"></div>`);
  bar.appendChild(h(`<div class="grow"></div>`));
  const done = h(`<button class="btn btn-primary">Done</button>`);
  done.onclick = ctx.close;
  bar.appendChild(done);
  body.appendChild(bar);
}

// ===========================================================================
// Setup wizard: model check, vault intro, macOS permission triggers
// ===========================================================================

async function openSetup() {
  App.state = "setupWizard";
  App.nav = "setup";
  App.error = null;
  render();
  await refreshStatus();
  if (App.state === "setupWizard") render();
}

async function recheckStatus() {
  // Snapshot which checks were failing so the rows that just turned green can
  // cross-fade into their new state instead of hard-swapping (presentation only).
  const was = {};
  ((App.status && App.status.checks) || []).forEach(c => { was[c.name] = !!c.ok; });
  await refreshStatus();
  App.checkWasFailing = was;
  if (App.state === "setupWizard") render();
}

function copyText(text, btn) {
  const done = () => { if (btn) { const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = old; }, 1400); } };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => {});
  }
}

function renderSetupWizard() {
  const s = App.status;
  el.appendChild(h(`<div class="setup-head ${ent(1)}">
    <h1>Welcome to Debrief</h1>
    <p class="setup-lead">A couple of quick checks so everything runs smoothly on this Mac. Nothing here leaves your computer.</p>
  </div>`));
  if (!s) {
    el.appendChild(h(`<div class="panel processing"><div class="spinner"></div><div class="label">Checking your setup...</div></div>`));
    refreshStatus().then(() => { if (App.state === "setupWizard") render(); });
    return;
  }
  ensureWizardState();
  el.appendChild(renderSetupStep1(s));
  // The cross-fade is for the render right after a Re-check only; clear the
  // snapshot so later repaints of the same screen sit still.
  App.checkWasFailing = null;
  el.appendChild(renderSetupStep2(s));
  el.appendChild(renderWizardProfession());
  el.appendChild(renderWizardFormat());
  el.appendChild(renderWizardFeatures());
  el.appendChild(renderSetupStep3());
  const bar = h(`<div class="actions-bar ${ent(8)} setup-finish">
    <div class="grow"></div>
    <button class="btn btn-primary" id="setupFinish">Finish setup</button>
  </div>`);
  bar.querySelector("#setupFinish").onclick = finishSetup;
  el.appendChild(bar);
  el.appendChild(h(`<div class="setup-foot">You can run setup again later from the sidebar.</div>`));
}

function setupCheckRow(c) {
  const cls = c.ok ? "ok" : (c.hard ? "fail" : "warn");
  const mark = c.ok ? "✓" : (c.hard ? "✕" : "!");
  // A row that flipped to ok on the last Re-check cross-fades into its new state.
  const changed = (App.checkWasFailing && App.checkWasFailing[c.name] === false && c.ok) ? " changed" : "";
  return h(`<div class="setup-check ${cls}${changed}">
    <span class="sc-mark">${mark}</span>
    <div class="sc-body">
      <div class="sc-name">${esc(c.name)}</div>
      <div class="sc-detail">${esc(c.detail || "")}</div>
      ${!c.ok && c.fix ? `<div class="sc-fix">${esc(c.fix)}</div>` : ""}
    </div>
  </div>`);
}

const LMS_LOAD_CMD = "lms load gemma-4-12b-it-qat --context-length 64000";

// Initialise the wizard's in-progress choices from current settings (or the
// defaults). These are collected across cards and POSTed in finishSetup before
// setup/complete, so the vault is configured the moment the app opens.
function ensureWizardState() {
  if (App.wizard) return;
  const s = App.settings || SETTINGS_DEFAULTS;
  App.wizard = {
    profession: s.profession || "therapy",
    note_format: s.note_format || "DAP",
    features: Object.assign({ calendar: true, email: true, verify: true, assistant: true }, s.features || {}),
    stt_engine: s.stt_engine || "parakeet",
  };
}

function renderSetupStep1(s) {
  const ready = !!s.ready;
  const reachable = (s.servers || []).filter(x => x.reachable);
  const card = h(`<div class="panel setup-card ${ent(2)}">
    <div class="setup-step-head">
      <span class="setup-num">1</span>
      <h2>Your local AI</h2>
      <span class="setup-badge ${ready ? "ok" : ""}">${ready ? "Ready" : "Needs attention"}</span>
    </div>
    <p class="setup-note">Debrief runs a private AI model on this Mac with LM Studio. Nothing is sent to the cloud.</p>
  </div>`);
  const checks = h(`<div class="setup-checks"></div>`);
  (s.checks || []).forEach(c => checks.appendChild(setupCheckRow(c)));
  card.appendChild(checks);
  if (reachable.length) {
    card.appendChild(h(`<div class="setup-sub">Model server reachable: ${esc(reachable.map(x => x.provider).join(", "))}</div>`));
  }
  const help = h(`<div class="setup-help">
    <a class="setup-link" href="https://lmstudio.ai" target="_blank" rel="noreferrer">Download LM Studio</a>
    <div class="setup-code-label">Then load the model (in LM Studio's terminal, or the lms command line):</div>
    <div class="setup-code"><code>${esc(LMS_LOAD_CMD)}</code><button class="copybtn" id="copyLms">Copy</button></div>
  </div>`);
  help.querySelector("#copyLms").onclick = (e) => copyText(LMS_LOAD_CMD, e.currentTarget);
  card.appendChild(help);

  // STT engine choice folded into the local-AI card.
  const w = App.wizard;
  const engHead = h(`<div class="setup-code-label" style="margin-top:16px">Transcription model</div>`);
  card.appendChild(engHead);
  const engBox = h(`<div class="set-radios"></div>`);
  STT_ENGINES.forEach(eng => {
    const row = h(`<label class="set-radio">
      <input type="radio" name="wizStt" value="${esc(eng.id)}" ${eng.id === w.stt_engine ? "checked" : ""} />
      <span class="set-radio-body"><span class="set-radio-title">${esc(eng.label)}</span><span class="set-radio-desc">${esc(eng.desc)}</span></span>
    </label>`);
    row.querySelector("input").onchange = () => { w.stt_engine = eng.id; };
    engBox.appendChild(row);
  });
  card.appendChild(engBox);
  card.appendChild(h(`<p class="setup-sub">${esc(STT_DOWNLOAD_HINT)}</p>`));

  const actions = h(`<div class="setup-actions"><button class="btn btn-ghost" id="recheck1">Re-check</button></div>`);
  actions.querySelector("#recheck1").onclick = recheckStatus;
  card.appendChild(actions);
  return card;
}

function wizardData() {
  const payload = App.settingsPayload || settingsFallbackPayload();
  return {
    professions: payload.professions || PROFESSIONS_FALLBACK,
    formats: payload.formats || FORMATS_FALLBACK,
  };
}

function renderWizardProfession() {
  const w = App.wizard;
  const { professions } = wizardData();
  const card = h(`<div class="panel setup-card ${ent(4)}">
    <div class="setup-step-head"><span class="setup-num">3</span><h2>Your profession</h2></div>
    <p class="setup-note">This sets the vocabulary Debrief expects and picks a sensible default note format.</p>
  </div>`);
  const sel = h(`<select class="set-select" id="wizProf"></select>`);
  professions.forEach(pf => {
    const o = h(`<option value="${esc(pf.id)}">${esc(pf.name)}</option>`);
    if (pf.id === w.profession) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = () => {
    w.profession = sel.value;
    const def = PROF_DEFAULT_FORMAT[w.profession];
    if (def) w.note_format = def;
    render();
  };
  card.appendChild(sel);
  const def = PROF_DEFAULT_FORMAT[w.profession];
  if (def) card.appendChild(h(`<p class="setup-sub">Default note format for this profession: ${esc(def)}. You can change it below.</p>`));
  return card;
}

function renderWizardFormat() {
  const w = App.wizard;
  const { formats } = wizardData();
  const card = h(`<div class="panel setup-card ${ent(5)}">
    <div class="setup-step-head"><span class="setup-num">4</span><h2>Your note format</h2></div>
    <p class="setup-note">Pick the structure your notes should follow, or import a sample of your own.</p>
  </div>`);
  const sel = h(`<select class="set-select" id="wizFmt"></select>`);
  formats.forEach(f => {
    const o = h(`<option value="${esc(f.id)}">${esc(f.name)}</option>`);
    if (f.id === w.note_format) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = () => { w.note_format = sel.value; };
  card.appendChild(sel);
  const importBtn = h(`<button class="btn btn-ghost set-import-btn" id="wizImport">or import a sample note</button>`);
  importBtn.onclick = () => launchImportFlow({
    profession: w.profession,
    fromWizard: true,
    onSaved: (saved) => { if (saved && saved.id) { w.note_format = saved.id; } render(); },
  });
  card.appendChild(importBtn);
  return card;
}

function renderWizardFeatures() {
  const w = App.wizard;
  const card = h(`<div class="panel setup-card ${ent(6)}">
    <div class="setup-step-head"><span class="setup-num">5</span><h2>What should Debrief do?</h2></div>
    <p class="setup-note">All on by default. Turn off anything you do not need. You can change this anytime in Settings.</p>
  </div>`);
  const togBox = h(`<div class="set-toggles"></div>`);
  FEATURE_ROWS.forEach(fr => {
    const on = w.features[fr.key] !== false;
    const row = h(`<label class="set-toggle">
      <span class="set-toggle-body"><span class="set-toggle-title">${esc(fr.label)}</span><span class="set-toggle-desc">${esc(fr.desc)}</span></span>
      <input type="checkbox" ${on ? "checked" : ""} />
    </label>`);
    row.querySelector("input").onchange = (e) => { w.features[fr.key] = e.target.checked; };
    togBox.appendChild(row);
  });
  card.appendChild(togBox);
  return card;
}

function renderSetupStep2(s) {
  const path = (s.vault && s.vault.path) || "";
  const card = h(`<div class="panel setup-card ${ent(3)}">
    <div class="setup-step-head"><span class="setup-num">2</span><h2>Your vault</h2></div>
    <p class="setup-note">Every client record, note, and worksheet is plain Markdown stored on this Mac. You own the files.</p>
    <div class="setup-code"><code>${esc(path)}</code><button class="copybtn" id="copyVault">Copy</button></div>
    <p class="setup-sub">To keep your vault somewhere else, set the DEBRIEF_VAULT_DIR environment variable to a folder you choose.</p>
    <p class="setup-sub">You can optionally open this folder in Obsidian to browse it visually. Debrief works fine without it.</p>
  </div>`);
  card.querySelector("#copyVault").onclick = (e) => copyText(path, e.currentTarget);
  return card;
}

const PERMS = [
  { key: "calendar", label: "Calendar", url: "/api/permissions/calendar", unlocks: "Lets Debrief book follow-up appointments in a dedicated Debrief calendar." },
  { key: "mail", label: "Mail", url: "/api/permissions/mail", unlocks: "Lets Debrief prepare email drafts to clients for your review. It never sends." },
  { key: "screen", label: "Screen Recording", url: "/api/permissions/screen", unlocks: "Lets Debrief read the screen to verify what it did." },
];

const PERM_ASKED = "We just asked macOS to show the permission prompt. Click Allow in the system dialog, then Re-check.";

function renderSetupStep3() {
  App.setupPerms = App.setupPerms || { calendar: null, mail: null, screen: null };
  const card = h(`<div class="panel setup-card ${ent(7)}">
    <div class="setup-step-head">
      <span class="setup-num">6</span>
      <h2>Mac permissions</h2>
      <button class="setup-skip" id="permSkip">Skip for now</button>
    </div>
    <p class="setup-note">These are only needed for calendar booking, email drafts, and on-screen verification. Grant them now, or skip and do it later.</p>
  </div>`);
  const rows = h(`<div class="perm-rows"></div>`);
  PERMS.forEach(p => rows.appendChild(buildPermRow(p)));
  card.appendChild(rows);
  card.querySelector("#permSkip").onclick = () => toast("You can grant permissions later from Setup in the sidebar.");
  return card;
}

function permClass(g) { return g === true ? "ok" : g === false ? "fail" : "warn"; }

function permMessage(res) {
  if (res.granted === true) return res.hint || "Access is granted.";
  return PERM_ASKED + (res.hint ? " " + res.hint : "");
}

function buildPermRow(p) {
  const res = (App.setupPerms || {})[p.key];
  const row = h(`<div class="perm-row">
    <div class="perm-info">
      <div class="perm-name">${esc(p.label)}</div>
      <div class="perm-unlocks">${esc(p.unlocks)}</div>
      ${res ? `<div class="perm-result ${permClass(res.granted)}">${esc(permMessage(res))}</div>` : ""}
    </div>
    <div class="perm-cta">
      <button class="btn btn-ghost perm-grant" id="grant-${esc(p.key)}">${res ? "Re-check" : "Grant"}</button>
    </div>
  </div>`);
  row.querySelector(".perm-grant").onclick = () => triggerPermission(p);
  return row;
}

async function triggerPermission(p) {
  const btn = document.getElementById("grant-" + p.key);
  if (btn) { btn.disabled = true; btn.textContent = "Asking..."; }
  App.setupPerms = App.setupPerms || {};
  try {
    const r = await fetch(p.url, { method: "POST" });
    App.setupPerms[p.key] = await r.json().catch(() => ({ granted: "unknown", hint: "No response from the app." }));
  } catch (e) {
    App.setupPerms[p.key] = { granted: "unknown", hint: e.message };
  }
  if (App.state === "setupWizard") render();
}

async function finishSetup() {
  // Persist the profession, format, features, and engine chosen in the wizard
  // BEFORE marking setup done, so the vault is configured from the first note.
  const w = App.wizard;
  if (w) {
    try {
      await postSettings({
        profession: w.profession,
        note_format: w.note_format,
        features: w.features,
        stt_engine: w.stt_engine,
      });
    } catch (e) { /* best effort; a bad value should not trap the user in setup */ }
  }
  try {
    await fetch("/api/setup/complete", { method: "POST" });
  } catch (e) { /* best effort; the app opens regardless */ }
  App.status = null;
  App.setupPerms = null;
  App.wizard = null;
  go("clients");
}

loadClients();
