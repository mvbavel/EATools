"use strict";
/*
 * EATools frontend. No build step, no dependencies. All DOM is built with
 * createElement/textContent — never innerHTML — because every rendered value is model
 * output derived from user files and must never be parsed as markup.
 *
 * This SHEETS table mirrors leanix.py / extract.py: one entry per entity type. Adding a
 * type means editing the schema, leanix.SHEETS, and this table in agreement.
 *
 * Flow: Input -> Matches (accept/reject proposed matches between sources) -> Review
 * (edit the merged entities) -> Output (select, preview Alfabet write-back, authorise,
 * export). The server is stateless: this page holds the per-source entities and the
 * decisions and posts them back to /api/merge.
 */
const SHEETS = {
  applications: {
    label: "Applications",
    fields: [
      { key: "name", label: "Name" },
      { key: "alias", label: "Alias" },
      { key: "description", label: "Description" },
      { key: "business_criticality", label: "Business Criticality" },
      { key: "lifecycle", label: "Lifecycle" },
      { key: "hosting", label: "Hosting" },
      { key: "capabilities", label: "Capabilities", list: true },
      { key: "data_objects", label: "Data Objects", list: true },
      { key: "it_components", label: "IT Components", list: true },
      { key: "evidence", label: "_evidence" },
      { key: "confidence", label: "_confidence", conf: true },
      { key: "_source", label: "_source" },
      { key: "_provenance", label: "_merged_from" },
      { key: "_conflicts", label: "_conflicts" },
      // Alfabet Reference of the matched record: drives Update vs Create on EDC export.
      { key: "_alfabet_ref", label: "_alfabet_ref" },
    ],
  },
  capabilities: {
    label: "Business Capabilities",
    fields: [
      { key: "name", label: "Name" },
      { key: "description", label: "Description" },
      { key: "level", label: "Level" },
      { key: "parent", label: "Parent" },
      { key: "evidence", label: "_evidence" },
      { key: "confidence", label: "_confidence", conf: true },
      { key: "_source", label: "_source" },
      { key: "_provenance", label: "_merged_from" },
      { key: "_conflicts", label: "_conflicts" },
    ],
  },
  it_components: {
    label: "IT Components",
    fields: [
      { key: "name", label: "Name" },
      { key: "description", label: "Description" },
      { key: "category", label: "Category" },
      { key: "evidence", label: "_evidence" },
      { key: "confidence", label: "_confidence", conf: true },
      { key: "_source", label: "_source" },
      { key: "_provenance", label: "_merged_from" },
      { key: "_conflicts", label: "_conflicts" },
    ],
  },
  data_objects: {
    label: "Data Objects",
    fields: [
      { key: "name", label: "Name" },
      { key: "description", label: "Description" },
      { key: "classification", label: "Classification" },
      { key: "evidence", label: "_evidence" },
      { key: "confidence", label: "_confidence", conf: true },
      { key: "_source", label: "_source" },
      { key: "_provenance", label: "_merged_from" },
      { key: "_conflicts", label: "_conflicts" },
    ],
  },
  interfaces: {
    label: "Interfaces",
    fields: [
      { key: "name", label: "Name" },
      { key: "description", label: "Description" },
      { key: "provider", label: "Provider" },
      { key: "consumer", label: "Consumer" },
      { key: "data_objects", label: "Data Objects", list: true },
      { key: "integration_type", label: "Integration Type" },
      { key: "frequency", label: "Frequency" },
      { key: "evidence", label: "_evidence" },
      { key: "confidence", label: "_confidence", conf: true },
      { key: "_source", label: "_source" },
      { key: "_provenance", label: "_merged_from" },
      { key: "_conflicts", label: "_conflicts" },
    ],
  },
};
const TYPE_KEYS = Object.keys(SHEETS);
const KEY_STORE = "eatools_key";
// Alfabet EDC workbooks are imported without a model call, so they need no API key.
const isEdc = (f) => /\.(xlsx|xlsm)$/i.test(f.name);

const state = {
  files: [],
  // The EDC workbook doubles as the export template; kept in memory only (stateless server).
  alfabetTemplate: null,
  credentials: false,
  savedKey: false, // server is using a key saved in the macOS Keychain
  keychain: false, // server can save one
  alfabet: false,
  // Matches
  sources: [], // per-source payloads, entities carry _id
  proposals: [],
  decisions: {}, // proposal id -> accepted | rejected | pending
  notes: [],
  blocked: new Set(), // pair keys the server refused (would join two Alfabet records)
  entityById: {},
  matchLimit: 150,
  matchesDirty: false,
  // Review
  payload: null,
  graph: null,
  mergeReport: [],
  meta: null,
  activeTab: TYPE_KEYS[0],
  reviewDirty: false,
  // Output
  selected: {}, // type -> Set of merged entity _id
  selTab: TYPE_KEYS[0],
  plan: null,
  planStale: true,
  planAuth: new Set(), // authorised plan row ids
};

// ---- small DOM helper -------------------------------------------------------
function el(tag, props, children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
  }
  for (const child of children || []) {
    if (child == null) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}
const $ = (id) => document.getElementById(id);

// ---- API key handling -------------------------------------------------------
function getKey() {
  return sessionStorage.getItem(KEY_STORE) || "";
}
function setupKeyField() {
  const input = $("api-key");
  input.value = getKey();
  input.addEventListener("input", () => {
    if (input.value) sessionStorage.setItem(KEY_STORE, input.value);
    else sessionStorage.removeItem(KEY_STORE);
    refreshAnalyseButton();
  });
  $("key-show").addEventListener("click", () => {
    input.type = input.type === "password" ? "text" : "password";
    $("key-show").textContent = input.type === "password" ? "Show" : "Hide";
  });
  $("key-clear").addEventListener("click", () => {
    input.value = "";
    sessionStorage.removeItem(KEY_STORE);
    refreshAnalyseButton();
  });

  // Opt-in persistence. The intent header makes the browser preflight, which the server
  // never approves cross-origin, so only this page can save or forget the key.
  $("key-save").addEventListener("click", async () => {
    const status = $("status");
    status.className = "";
    status.textContent = "Checking the key with Anthropic…";
    try {
      const res = await fetch("/api/key", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-EATools-Intent": "save-key" },
        body: JSON.stringify({ key: input.value.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Saving failed (${res.status})`);
      input.value = "";
      sessionStorage.removeItem(KEY_STORE);
      state.credentials = true;
      state.savedKey = true;
      status.textContent = "Key saved to the Keychain; it will be used whenever EATools starts.";
    } catch (err) {
      status.className = "error";
      status.textContent = err.message || String(err);
    }
    renderKeyUi();
  });
  $("key-forget").addEventListener("click", async () => {
    const res = await fetch("/api/key", { method: "DELETE", headers: { "X-EATools-Intent": "forget-key" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("status").className = "error";
      $("status").textContent = data.detail || `Forget failed (${res.status})`;
      return;
    }
    state.savedKey = false;
    state.credentials = !!data.credentials;
    $("status").className = "";
    $("status").textContent = "Saved key removed from the Keychain.";
    renderKeyUi();
  });
}

function renderKeyUi() {
  $("key-field").hidden = state.credentials;
  $("key-save").hidden = !state.keychain;
  $("saved-key").hidden = !state.savedKey;
  refreshAnalyseButton();
}

// ---- file selection ---------------------------------------------------------
function addFiles(fileList) {
  for (const f of fileList) state.files.push(f);
  renderFileList();
  refreshAnalyseButton();
}
function renderFileList() {
  const ul = $("file-list");
  ul.textContent = "";
  state.files.forEach((f, i) => {
    ul.appendChild(
      el("li", null, [
        el("span", { text: `${f.name} (${Math.round(f.size / 1024)} KB)` }),
        el("button", {
          title: "Remove",
          onclick: () => {
            state.files.splice(i, 1);
            renderFileList();
            refreshAnalyseButton();
          },
        }, ["×"]),
      ])
    );
  });
}
function alfabetSelected() {
  return state.alfabet && $("alfabet-import").checked;
}
function refreshAnalyseButton() {
  const haveCreds = state.credentials || !!getKey() || state.files.every(isEdc);
  $("analyse").disabled = !((state.files.length > 0 || alfabetSelected()) && haveCreds);
}

// ---- main tabs ----------------------------------------------------------------
const MAIN_TABS = ["input", "matches", "review", "output"];

function showTab(name) {
  for (const t of MAIN_TABS) $(`tab-${t}`).hidden = t !== name;
  for (const b of document.querySelectorAll("#main-tabs button")) b.classList.toggle("active", b.dataset.tab === name);
  if (name === "matches") renderMatches();
  if (name === "review") renderReview();
  if (name === "output") renderOutput();
}
function enableTabs(on) {
  for (const b of document.querySelectorAll("#main-tabs button")) if (b.dataset.tab !== "input") b.disabled = !on;
}
function setupMainTabs() {
  for (const b of document.querySelectorAll("#main-tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
}

function setStatus(id, message, isError) {
  const node = $(id);
  node.className = isError ? "error" : "hint";
  node.textContent = message || "";
}

// ---- analyse ----------------------------------------------------------------
async function analyse() {
  const btn = $("analyse");
  btn.disabled = true;
  const parts = [];
  if (state.files.length) parts.push(`${state.files.length} file(s)`);
  if (alfabetSelected()) parts.push("Alfabet selection");
  setStatus("status", `Analysing ${parts.join(" + ")}… this can take a minute.`);

  const form = new FormData();
  for (const f of state.files) form.append("files", f);
  form.append("context", $("context").value || "");
  if (alfabetSelected()) {
    form.append("alfabet_import", "true");
    for (const k of ["company", "name", "version", "objectstate"]) form.append(`alfabet_${k}`, $(`alfabet-${k}`).value || "");
  }

  const headers = {};
  const key = getKey();
  if (key) headers["X-Anthropic-Api-Key"] = key;

  try {
    const res = await fetch("/api/analyse", { method: "POST", body: form, headers });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Request failed (${res.status})`);
    }
    const data = await res.json();
    state.alfabetTemplate = state.files.find(isEdc) || state.alfabetTemplate;
    handleAnalysis(data);
    setStatus("status", "");
  } catch (err) {
    setStatus("status", err.message || String(err), true);
  }
  refreshAnalyseButton();
}

function handleAnalysis(data) {
  state.sources = data._source_payloads || [];
  state.proposals = data._proposals || [];
  state.decisions = Object.fromEntries(state.proposals.map((p) => [p.id, p.status]));
  state.notes = data._match_notes || [];
  state.entityById = {};
  for (const src of state.sources) {
    for (const t of TYPE_KEYS) for (const e of src[t] || []) state.entityById[e._id] = { type: t, ent: e };
  }
  state.meta = {
    sources: data._sources || [],
    skipped: data._skipped || [],
    usage: data._usage || {},
    summary: data.diagram_summary || "",
  };
  state.matchLimit = 150;
  state.matchesDirty = false;
  loadMerged(data);
  enableTabs(true);
  $("start-over").hidden = false;
  showTab(state.proposals.length ? "matches" : "review");
}

// A merged result (from /api/analyse or /api/merge) replaces the review and resets output.
function loadMerged(data) {
  state.graph = data._graph || { nodes: [], edges: [] };
  state.mergeReport = data._merge_report || [];
  state.blocked = new Set((data._blocked || []).map(([a, b]) => pairKey(a, b)));
  const payload = { diagram_summary: data.diagram_summary || "", open_questions: (data.open_questions || []).slice() };
  for (const t of TYPE_KEYS) payload[t] = (data[t] || []).map((e) => Object.assign({}, e));
  state.payload = payload;
  state.activeTab = TYPE_KEYS[0];
  state.reviewDirty = false;
  resetSelection();
  state.plan = null;
  state.planStale = true;
  state.planAuth = new Set();
  resetAuthorisation();
}

const pairKey = (a, b) => [a, b].sort().join("|");

// ---- matches ----------------------------------------------------------------
const METHOD_LABEL = { exact: "Exact name", alias: "Alias / short name", fuzzy: "Fuzzy" };
const DIFF_FIELDS = ["description", "business_criticality", "lifecycle", "hosting", "category", "classification", "level", "parent"];

function setupMatches() {
  const typeSel = $("mf-type");
  typeSel.appendChild(el("option", { value: "", text: "All" }));
  for (const t of TYPE_KEYS) typeSel.appendChild(el("option", { value: t, text: SHEETS[t].label }));
  for (const id of ["mf-type", "mf-status", "mf-method", "mf-score", "mf-search"]) {
    $(id).addEventListener("input", () => { state.matchLimit = 150; renderMatches(); });
  }
  const bulk = (fn) => () => { for (const p of filteredProposals()) fn(p); state.matchesDirty = true; renderMatches(); };
  $("mb-accept").addEventListener("click", bulk((p) => { state.decisions[p.id] = "accepted"; }));
  $("mb-reject").addEventListener("click", bulk((p) => { state.decisions[p.id] = "rejected"; }));
  $("mb-reset").addEventListener("click", bulk((p) => { state.decisions[p.id] = p.status; }));
  $("apply-matches").addEventListener("click", applyMatches);
}

function entityOf(id) {
  return (state.entityById[id] || {}).ent || { name: id };
}

function filteredProposals() {
  const type = $("mf-type").value;
  const status = $("mf-status").value;
  const method = $("mf-method").value;
  const minScore = (Number($("mf-score").value) || 0) / 100;
  const search = $("mf-search").value.trim().toLowerCase();
  return state.proposals.filter((p) => {
    if (type && p.type !== type) return false;
    if (status && state.decisions[p.id] !== status) return false;
    if (method && p.method !== method) return false;
    if (p.score < minScore) return false;
    if (search) {
      const names = `${entityOf(p.a).name} ${entityOf(p.b).name}`.toLowerCase();
      if (!names.includes(search)) return false;
    }
    return true;
  });
}

function renderMatches() {
  const counts = { accepted: 0, rejected: 0, pending: 0 };
  for (const p of state.proposals) counts[state.decisions[p.id]] = (counts[state.decisions[p.id]] || 0) + 1;

  const summary = $("match-summary");
  summary.textContent = "";
  summary.appendChild(el("h2", { text: `Matches between sources (${state.proposals.length})` }));
  summary.appendChild(el("p", { class: "hint", text:
    "Exact and unambiguous alias matches start accepted; fuzzy matches wait for you unless Claude confirmed them. " +
    "Two different Alfabet records are never offered as the same thing." }));
  summary.appendChild(el("div", { class: "chips" }, [
    el("span", { class: "meta-chip st-accepted", text: `Accepted ${counts.accepted}` }),
    el("span", { class: "meta-chip st-pending", text: `Pending ${counts.pending}` }),
    el("span", { class: "meta-chip st-rejected", text: `Rejected ${counts.rejected}` }),
    state.blocked.size ? el("span", { class: "meta-chip st-blocked", text: `Blocked ${state.blocked.size}` }) : null,
  ]));

  const notes = $("match-notes");
  notes.textContent = "";
  if (state.notes.length) {
    notes.appendChild(el("div", { class: "panel notes" }, [
      el("h2", { text: "Needs your decision" }),
      el("ul", null, state.notes.map((n) => el("li", { text: n }))),
    ]));
  }

  const list = $("match-list");
  list.textContent = "";
  const shown = filteredProposals();
  if (!shown.length) {
    list.appendChild(el("div", { class: "empty", text: state.proposals.length ? "No matches for these filters." : "No matches were proposed — every entity is unique." }));
  }
  for (const p of shown.slice(0, state.matchLimit)) list.appendChild(proposalCard(p));
  if (shown.length > state.matchLimit) {
    list.appendChild(el("button", {
      class: "secondary",
      onclick: () => { state.matchLimit += 150; renderMatches(); },
    }, [`Show more (${shown.length - state.matchLimit} remaining)`]));
  }
  setStatus("apply-status", state.matchesDirty ? "Decisions changed — apply them to rebuild the review." : "");
}

function proposalCard(p) {
  const a = entityOf(p.a), b = entityOf(p.b);
  const decision = state.decisions[p.id];
  const differing = new Set(DIFF_FIELDS.filter((f) => {
    const va = valueText(a[f]), vb = valueText(b[f]);
    return va && vb && va !== vb;
  }));
  const decide = (value) => () => { state.decisions[p.id] = value; state.matchesDirty = true; renderMatches(); };

  return el("div", { class: `match-card dec-${decision}` }, [
    el("div", { class: "match-head" }, [
      el("span", { class: "badge", text: SHEETS[p.type].label }),
      el("span", { class: "score", text: `${Math.round(p.score * 100)}%` }),
      el("span", { class: "badge", text: METHOD_LABEL[p.method] || p.method }),
      p.ai ? el("span", { class: `badge ai-${p.ai}`, text: `Claude: ${p.ai}` }) : null,
      state.blocked.has(pairKey(p.a, p.b))
        ? el("span", { class: "badge st-blocked", text: "Blocked: would join two Alfabet records" }) : null,
      el("span", { class: "grow" }),
      el("button", { class: decision === "accepted" ? "" : "secondary", onclick: decide("accepted") }, ["Accept"]),
      el("button", { class: decision === "rejected" ? "danger" : "secondary", onclick: decide("rejected") }, ["Reject"]),
    ]),
    el("div", { class: "match-body" }, [entityBox(a, differing), el("div", { class: "match-arrow", text: "↔" }), entityBox(b, differing)]),
    el("div", { class: "hint", text: p.reason }),
  ]);
}

function valueText(v) {
  if (v == null || v === "unknown") return "";
  return String(v).trim();
}

function entityBox(e, differing) {
  const origin = (e._origin || "diagram").includes("alfabet") ? "Alfabet" : "Diagram";
  const facts = DIFF_FIELDS.filter((f) => f !== "description" && valueText(e[f]))
    .map((f) => el("span", { class: differing.has(f) ? "fact diff" : "fact", text: `${f.replace(/_/g, " ")}: ${valueText(e[f])}` }));
  const desc = valueText(e.description);
  return el("div", { class: "entity-box" }, [
    el("div", null, [el("strong", { text: e.name || "(unnamed)" }), " ", el("span", { class: `badge origin-${origin.toLowerCase()}`, text: origin })]),
    el("div", { class: "prov", text: [e._source, e._alfabet_ref ? `Ref ${e._alfabet_ref}` : ""].filter(Boolean).join(" · ") }),
    facts.length ? el("div", { class: "facts" }, facts) : null,
    // Descriptions are worded per source and nearly always differ; only attributes are flagged.
    desc ? el("div", { class: "desc", text: desc.length > 180 ? desc.slice(0, 179) + "…" : desc }) : null,
  ]);
}

async function applyMatches() {
  if (state.reviewDirty && !window.confirm("Applying match decisions rebuilds the review and discards edits made there. Continue?")) return;
  const accepted = state.proposals.filter((p) => state.decisions[p.id] === "accepted").map((p) => [p.a, p.b]);
  setStatus("apply-status", "Applying decisions…");
  try {
    const res = await fetch("/api/merge", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ sources: state.sources, accepted }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Merge failed (${res.status})`);
    loadMerged(data);
    state.matchesDirty = false;
    if (state.blocked.size) {
      showTab("matches");
      $("mf-status").value = "";
      renderMatches();
      setStatus("apply-status", `${state.blocked.size} accepted match(es) were blocked because they would join two Alfabet records — reject one of each pair.`, true);
      return;
    }
    showTab("review");
  } catch (err) {
    setStatus("apply-status", err.message || String(err), true);
  }
}

// ---- review -----------------------------------------------------------------
function renderReview() {
  if (!state.payload) return;
  renderSummary();
  renderMeta();
  renderTabs();
  renderTabContent();
  renderQuestions();
  renderMergeReport();
}

function renderSummary() {
  const panel = $("summary-panel");
  panel.textContent = "";
  if (state.matchesDirty) {
    panel.appendChild(el("p", { class: "error", text: "Match decisions changed and are not applied yet — use “Apply decisions” in Matches." }));
  }
  panel.appendChild(el("h2", { text: "Diagram summary" }));
  panel.appendChild(el("p", { text: state.payload.diagram_summary || state.meta.summary || "(no summary)" }));
}

function renderMeta() {
  const panel = $("meta-panel");
  panel.textContent = "";
  const u = state.meta.usage;
  panel.appendChild(el("span", { class: "meta-chip", text: `Sources: ${state.meta.sources.map((s) => s.name).join(", ") || "—"}` }));
  if (state.meta.skipped.length) {
    panel.appendChild(el("span", { class: "meta-chip", text: `Skipped: ${state.meta.skipped.map((s) => `${s.name} (${s.reason})`).join("; ")}` }));
  }
  const tokens = (u.input_tokens || 0) + (u.output_tokens || 0);
  panel.appendChild(el("span", { class: "meta-chip", text: `Tokens: ${tokens} (in ${u.input_tokens || 0} / out ${u.output_tokens || 0})` }));
}

function renderTabs() {
  const tabs = $("tabs");
  tabs.textContent = "";
  for (const t of TYPE_KEYS) {
    const count = (state.payload[t] || []).length;
    tabs.appendChild(
      el("button", {
        class: state.activeTab === t ? "active" : "",
        onclick: () => { state.activeTab = t; renderTabs(); renderTabContent(); },
      }, [`${SHEETS[t].label} (${count})`])
    );
  }
  tabs.appendChild(
    el("button", {
      class: state.activeTab === "_graph" ? "active" : "",
      onclick: () => { state.activeTab = "_graph"; renderTabs(); renderTabContent(); },
    }, [`Graph (${state.graph.nodes.length})`])
  );
}

function renderTabContent() {
  const container = $("tab-content");
  container.textContent = "";
  if (state.activeTab === "_graph") {
    container.appendChild(renderGraph());
    return;
  }
  container.appendChild(renderTable(state.activeTab));
}

// Rebuild the table element on each render; never cache a detached node.
function renderTable(type) {
  const spec = SHEETS[type];
  const rows = state.payload[type] || [];
  if (!rows.length) return el("div", { class: "empty", text: "No entities of this type." });

  const thead = el("tr", null, [
    ...spec.fields.map((f) => el("th", { text: f.label })),
    el("th", { text: "" }),
  ]);

  const body = el("tbody");
  rows.forEach((row, rowIdx) => {
    const tr = el("tr");
    for (const field of spec.fields) {
      const td = el("td");
      const value = field.list ? (row[field.key] || []).join("; ") : (row[field.key] == null ? "" : String(row[field.key]));
      const input = el("input", { type: "text", value: value });
      if (field.conf) input.className = "conf-" + (row[field.key] || "");
      input.addEventListener("input", () => {
        if (field.list) row[field.key] = input.value.split(";").map((s) => s.trim()).filter(Boolean);
        else row[field.key] = input.value;
        if (field.conf) input.className = "conf-" + input.value;
        markReviewEdited();
      });
      td.appendChild(input);
      tr.appendChild(td);
    }
    tr.appendChild(
      el("td", null, [
        el("button", {
          class: "row-del",
          title: "Delete row",
          onclick: () => { rows.splice(rowIdx, 1); markReviewEdited(); renderTabs(); renderTabContent(); },
        }, ["×"]),
      ])
    );
    body.appendChild(tr);
  });

  return el("div", { class: "table-wrap" }, [el("table", null, [el("thead", null, [thead]), body])]);
}

// An edit changes what would be exported, so any earlier preview/authorisation is void.
function markReviewEdited() {
  state.reviewDirty = true;
  state.planStale = true;
  resetAuthorisation();
}

// Graph: a simple SVG layout plus a readable node/edge list with provenance.
function renderGraph() {
  const wrap = el("div");
  const g = state.graph;
  if (!g.nodes.length) return el("div", { class: "empty", text: "No graph nodes." });

  const W = 900, H = 440, cx = W / 2, cy = H / 2, r = Math.min(W, H) / 2 - 60;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "graph-svg");

  const pos = {};
  g.nodes.forEach((n, i) => {
    const a = (2 * Math.PI * i) / g.nodes.length;
    pos[n.id] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  });
  const colors = { application: "#3b5bdb", capability: "#2f9e44", it_component: "#e8590c", data_object: "#9c36b5", interface: "#1098ad" };

  for (const e of g.edges) {
    const p1 = pos[e.source], p2 = pos[e.target];
    if (!p1 || !p2) continue;
    const line = document.createElementNS(svg.namespaceURI, "line");
    line.setAttribute("x1", p1.x); line.setAttribute("y1", p1.y);
    line.setAttribute("x2", p2.x); line.setAttribute("y2", p2.y);
    svg.appendChild(line);
  }
  for (const n of g.nodes) {
    const p = pos[n.id];
    const c = document.createElementNS(svg.namespaceURI, "circle");
    c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); c.setAttribute("r", 7);
    c.setAttribute("fill", colors[n.type] || "#888");
    const title = document.createElementNS(svg.namespaceURI, "title");
    title.textContent = `${n.name} [${n.type}]${n.sources ? " — " + n.sources : ""}`;
    c.appendChild(title);
    svg.appendChild(c);
    const label = document.createElementNS(svg.namespaceURI, "text");
    label.setAttribute("x", p.x + 9); label.setAttribute("y", p.y + 3);
    label.textContent = n.name.length > 22 ? n.name.slice(0, 21) + "…" : n.name;
    svg.appendChild(label);
  }
  wrap.appendChild(svg);

  const list = el("div", { class: "graph-list" });
  list.appendChild(el("h2", { text: "Nodes & provenance" }));
  const edgesByNode = {};
  for (const e of g.edges) (edgesByNode[e.source] = edgesByNode[e.source] || []).push(e);
  for (const n of g.nodes) {
    const outs = (edgesByNode[n.id] || []).map((e) => `${e.relation} → ${e.target_name}`);
    list.appendChild(
      el("div", { class: "graph-node" }, [
        el("span", { text: `${n.name} ` }),
        el("span", { class: "badge", text: n.type }),
        outs.length ? el("span", { text: "  " + outs.join(", ") }) : null,
        n.sources ? el("div", { class: "prov", text: `from: ${n.sources}${n.merged_from ? "  (merged: " + n.merged_from + ")" : ""}` }) : null,
      ])
    );
  }
  wrap.appendChild(list);
  return wrap;
}

function renderQuestions() {
  const panel = $("questions-panel");
  panel.textContent = "";
  const qs = state.payload.open_questions || [];
  panel.appendChild(el("h2", { text: `Open questions (${qs.length})` }));
  if (!qs.length) { panel.appendChild(el("p", { class: "hint", text: "None flagged." })); return; }
  panel.appendChild(el("ul", null, qs.map((q) => el("li", { text: q }))));
}

function renderMergeReport() {
  const panel = $("merge-panel");
  panel.textContent = "";
  panel.appendChild(el("h2", { text: `Merges (${state.mergeReport.length})` }));
  if (!state.mergeReport.length) { panel.appendChild(el("p", { class: "hint", text: "No merges — each entity came from a single name." })); return; }
  for (const m of state.mergeReport) {
    panel.appendChild(
      el("div", { class: "merge-item", text: `[${m.type}] "${m.canonical}" ← ${m.merged_from.join(", ")}  (sources: ${m.sources})` })
    );
  }
}

// ---- output: selection --------------------------------------------------------
const SINGULAR = { applications: "application", capabilities: "capability", it_components: "it_component", data_objects: "data_object", interfaces: "interface" };
const fromDiagram = (e) => (e._origin || "diagram").includes("diagram");
const fromAlfabet = (e) => (e._origin || "").includes("alfabet");

function itemStatus(e) {
  if (fromDiagram(e) && e._alfabet_ref) return "Matched to Alfabet";
  if (fromDiagram(e) && fromAlfabet(e)) return "Diagram + Alfabet";
  if (fromDiagram(e)) return "New";
  return "Alfabet only";
}

// Default: what the diagrams contributed. Alfabet-only records are reference data, so
// they start unselected -- unless there are no diagrams at all (a pure Alfabet export).
function resetSelection() {
  const anyDiagram = TYPE_KEYS.some((t) => (state.payload[t] || []).some(fromDiagram));
  state.selected = {};
  for (const t of TYPE_KEYS) {
    state.selected[t] = new Set((state.payload[t] || []).filter((e) => !anyDiagram || fromDiagram(e)).map((e) => e._id));
  }
}

function selectionChanged() {
  state.planStale = true;
  resetAuthorisation();
  renderSelectionTabs();
  renderPlan();
  renderOutputSummary();
}

function setupOutput() {
  $("sel-search").addEventListener("input", renderSelectionTable);
  const bulk = (on) => () => {
    for (const e of shownSelectionRows()) (on ? state.selected[state.selTab].add(e._id) : state.selected[state.selTab].delete(e._id));
    renderSelectionTable();
    selectionChanged();
  };
  $("sel-all").addEventListener("click", bulk(true));
  $("sel-none").addEventListener("click", bulk(false));
  $("sel-default").addEventListener("click", () => { resetSelection(); renderSelectionTable(); selectionChanged(); });

  const picker = $("alfabet-template-input");
  $("choose-template").addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    if (picker.files.length) {
      state.alfabetTemplate = picker.files[0];
      state.planStale = true;
      resetAuthorisation();
      renderOutput();
    }
    picker.value = "";
  });
  $("preview-alfabet").addEventListener("click", previewAlfabet);

  $("auth-by").addEventListener("input", refreshExportButtons);
  $("auth-confirm").addEventListener("change", refreshExportButtons);
  $("export-leanix").addEventListener("click", exportLeanix);
  $("export-alfabet").addEventListener("click", exportAlfabet);
  $("export-graph-json").addEventListener("click", () =>
    download("/api/export/graph?format=json", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ graph: selectedGraph() }) }, "graph.json"));
  $("export-graph-graphml").addEventListener("click", () =>
    download("/api/export/graph?format=graphml", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ graph: selectedGraph() }) }, "graph.graphml"));
}

function renderOutput() {
  if (!state.payload) return;
  renderSelectionTabs();
  renderSelectionTable();
  renderPlan();
  renderOutputSummary();
  refreshExportButtons();
}

function renderSelectionTabs() {
  const tabs = $("sel-tabs");
  tabs.textContent = "";
  for (const t of TYPE_KEYS) {
    const total = (state.payload[t] || []).length;
    tabs.appendChild(el("button", {
      class: state.selTab === t ? "active" : "",
      onclick: () => { state.selTab = t; renderSelectionTabs(); renderSelectionTable(); },
    }, [`${SHEETS[t].label} (${state.selected[t].size}/${total})`]));
  }
}

function shownSelectionRows() {
  const search = $("sel-search").value.trim().toLowerCase();
  return (state.payload[state.selTab] || []).filter((e) => !search || (e.name || "").toLowerCase().includes(search));
}

function renderSelectionTable() {
  const wrap = $("sel-table");
  wrap.textContent = "";
  const rows = shownSelectionRows();
  if (!rows.length) { wrap.appendChild(el("div", { class: "empty", text: "Nothing to show." })); return; }
  const selected = state.selected[state.selTab];
  const body = el("tbody");
  for (const e of rows) {
    const box = el("input", { type: "checkbox" });
    box.checked = selected.has(e._id);
    box.addEventListener("change", () => {
      if (box.checked) selected.add(e._id); else selected.delete(e._id);
      selectionChanged();
    });
    const status = itemStatus(e);
    body.appendChild(el("tr", null, [
      el("td", null, [box]),
      el("td", { text: e.name }),
      el("td", null, [el("span", { class: `badge status-${status.split(" ")[0].toLowerCase()}`, text: status })]),
      el("td", { class: "prov", text: e._source || "" }),
      el("td", { class: "conf-" + (e.confidence || ""), text: e.confidence || "" }),
      el("td", { text: e._alfabet_ref || "" }),
    ]));
  }
  const head = el("tr", null, ["", "Name", "Status", "Sources", "Confidence", "Alfabet ref"].map((h) => el("th", { text: h })));
  wrap.appendChild(el("div", { class: "table-wrap" }, [el("table", { class: "select-table" }, [el("thead", null, [head]), body])]));
}

function selectedPayload() {
  const p = { diagram_summary: state.payload.diagram_summary, open_questions: state.payload.open_questions };
  for (const t of TYPE_KEYS) p[t] = (state.payload[t] || []).filter((e) => state.selected[t].has(e._id));
  return p;
}

function selectedGraph() {
  const names = {};
  for (const t of TYPE_KEYS) names[SINGULAR[t]] = new Set(selectedPayload()[t].map((e) => e.name));
  const nodes = state.graph.nodes.filter((n) => names[n.type] && names[n.type].has(n.name));
  const ids = new Set(nodes.map((n) => n.id));
  return { nodes, edges: state.graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
}

// ---- output: Alfabet write-back preview -----------------------------------------
async function previewAlfabet() {
  if (!state.alfabetTemplate) { $("alfabet-template-input").click(); return; }
  setStatus("output-status", "Previewing Alfabet changes…");
  const form = new FormData();
  form.append("template", state.alfabetTemplate);
  form.append("body", new Blob([JSON.stringify({ payload: selectedPayload() })], { type: "application/json" }), "payload.json");
  try {
    const res = await fetch("/api/alfabet/plan", { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Preview failed (${res.status})`);
    state.plan = data;
    state.planStale = false;
    state.planAuth = new Set(data.rows.filter((r) => r.operation === "Create" || r.operation === "Update").map((r) => r.id));
    resetAuthorisation();
    setStatus("output-status", "");
  } catch (err) {
    setStatus("output-status", err.message || String(err), true);
  }
  renderPlan();
  renderOutputSummary();
}

function renderPlan() {
  $("template-status").textContent = state.alfabetTemplate
    ? `Template: ${state.alfabetTemplate.name}`
    : "No EDC template yet — choose the Alfabet EDC export (e.g. Application_All_….xlsx) to write back to.";
  $("preview-alfabet").disabled = !state.alfabetTemplate;

  const wrap = $("plan-table");
  wrap.textContent = "";
  if (!state.plan) {
    wrap.appendChild(el("p", { class: "hint", text: "Preview to see exactly what would be created or updated in Alfabet for the selected applications." }));
    return;
  }
  const c = state.plan.counts;
  if (state.planStale) {
    wrap.appendChild(el("p", { class: "error", text: "The selection, review or template changed since this preview — preview again before exporting to Alfabet." }));
  }
  wrap.appendChild(el("p", { class: "hint", text:
    `${c.Create} create · ${c.Update} update · ${c.Unchanged} matched and unchanged (not written) · ${c.Skipped} skipped. Tick the rows you authorise.` }));
  if (!state.plan.rows.length) return;

  const body = el("tbody");
  for (const r of state.plan.rows) {
    const writable = r.operation === "Create" || r.operation === "Update";
    const box = el("input", { type: "checkbox", disabled: !writable || state.planStale });
    box.checked = writable && state.planAuth.has(r.id);
    box.addEventListener("change", () => {
      if (box.checked) state.planAuth.add(r.id); else state.planAuth.delete(r.id);
      resetAuthorisation();
      renderOutputSummary();
    });
    body.appendChild(el("tr", null, [
      el("td", null, [box]),
      el("td", { text: r.name }),
      el("td", null, [el("span", { class: `badge op-${r.operation.toLowerCase()}`, text: r.operation })]),
      el("td", { text: r.reference || "" }),
      el("td", null, [el("ul", { class: "plan-notes" }, (r.notes.length ? r.notes : ["ok"]).map((n) => el("li", { text: n })))]),
    ]));
  }
  const head = el("tr", null, ["Authorise", "Application", "Operation", "Reference", "Changes / issues"].map((h) => el("th", { text: h })));
  wrap.appendChild(el("div", { class: "table-wrap" }, [el("table", null, [el("thead", null, [head]), body])]));
}

// ---- output: authorise & export ---------------------------------------------------
// Anything that changes what would be exported withdraws the confirmation, so an
// authorisation always covers exactly what was last reviewed.
function resetAuthorisation() {
  const box = $("auth-confirm");
  if (box) box.checked = false;
  refreshExportButtons();
}

function authorised() {
  return $("auth-by").value.trim() !== "" && $("auth-confirm").checked;
}

function refreshExportButtons() {
  if (!state.payload) return;
  const anySelected = TYPE_KEYS.some((t) => state.selected[t] && state.selected[t].size);
  $("export-leanix").disabled = !(authorised() && anySelected);
  $("export-alfabet").disabled = !(authorised() && state.plan && !state.planStale && state.planAuth.size && state.alfabetTemplate);
}

function renderOutputSummary() {
  const sel = selectedPayload();
  const parts = TYPE_KEYS.map((t) => `${sel[t].length} ${SHEETS[t].label.toLowerCase()}`);
  let text = `LeanIX export: ${parts.join(", ")}.`;
  if (state.plan && !state.planStale) {
    const rows = state.plan.rows.filter((r) => state.planAuth.has(r.id));
    const creates = rows.filter((r) => r.operation === "Create").length;
    text += ` Alfabet write-back: ${creates} create + ${rows.length - creates} update authorised.`;
  } else {
    text += " Alfabet write-back: preview first.";
  }
  $("output-summary").textContent = text;
}

function authorisationBody() {
  return { by: $("auth-by").value.trim() };
}

function exportLeanix() {
  download("/api/export", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ payload: selectedPayload(), graph: selectedGraph(), authorisation: authorisationBody() }),
  }, "eatools_export.zip");
}

function exportAlfabet() {
  const body = { payload: selectedPayload(), authorised_ids: [...state.planAuth], authorisation: authorisationBody() };
  const form = new FormData();
  form.append("template", state.alfabetTemplate);
  // As a file part: plain form fields are capped at 1 MB server-side.
  form.append("body", new Blob([JSON.stringify(body)], { type: "application/json" }), "payload.json");
  download("/api/export/alfabet", { method: "POST", body: form }, "alfabet_export.zip");
}

async function download(url, opts, fallbackName) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    setStatus("output-status", detail.detail || `Export failed (${res.status})`, true);
    return;
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const match = /filename="([^"]+)"/.exec(cd);
  const a = el("a", { href: URL.createObjectURL(blob), download: match ? match[1] : fallbackName });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  setStatus("output-status", `Downloaded ${match ? match[1] : fallbackName}.`);
}
const JSON_HEADERS = { "Content-Type": "application/json" };

// ---- init -------------------------------------------------------------------
function setupDropzone() {
  const dz = $("dropzone");
  const fi = $("file-input");
  dz.addEventListener("click", () => fi.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") fi.click(); });
  fi.addEventListener("change", () => { addFiles(fi.files); fi.value = ""; });
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove("drag")));
  dz.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files); });
}

function startOver() {
  state.files = [];
  state.sources = [];
  state.proposals = [];
  state.payload = null;
  state.plan = null;
  state.alfabetTemplate = null;
  renderFileList();
  enableTabs(false);
  $("start-over").hidden = true;
  setStatus("status", "");
  showTab("input");
  refreshAnalyseButton();
}

async function init() {
  setupMainTabs();
  setupDropzone();
  setupKeyField();
  setupMatches();
  setupOutput();
  $("analyse").addEventListener("click", analyse);
  $("start-over").addEventListener("click", startOver);
  $("to-output").addEventListener("click", () => showTab("output"));
  try {
    const res = await fetch("/api/health");
    const h = await res.json();
    state.credentials = !!h.credentials;
    state.savedKey = !!h.saved_key;
    state.keychain = !!h.keychain;
    state.alfabet = !!h.alfabet;
  } catch (_) {
    state.credentials = false;
    state.alfabet = false;
  }
  renderKeyUi();
  // Alfabet credentials live server-side only; the panel appears when the server has them.
  $("alfabet-field").hidden = !state.alfabet;
  $("alfabet-import").addEventListener("change", refreshAnalyseButton);
}

init();
