"use strict";
/*
 * EATools frontend. No build step, no dependencies. All DOM is built with
 * createElement/textContent — never innerHTML — because every rendered value is model
 * output derived from user files and must never be parsed as markup.
 *
 * This SHEETS table mirrors leanix.py / extract.py: one entry per entity type. Adding a
 * type means editing the schema, leanix.SHEETS, and this table in agreement.
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

const state = {
  files: [],
  credentials: false,
  payload: null,
  graph: null,
  mergeReport: [],
  meta: null,
  activeTab: TYPE_KEYS[0],
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
function refreshAnalyseButton() {
  const haveCreds = state.credentials || !!getKey();
  $("analyse").disabled = !(state.files.length > 0 && haveCreds);
}

// ---- analyse ----------------------------------------------------------------
async function analyse() {
  const btn = $("analyse");
  const status = $("status");
  btn.disabled = true;
  status.className = "";
  status.textContent = `Analysing ${state.files.length} file(s)… this can take a minute.`;

  const form = new FormData();
  for (const f of state.files) form.append("files", f);
  form.append("context", $("context").value || "");

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
    loadPayload(data);
  } catch (err) {
    status.className = "error";
    status.textContent = err.message || String(err);
    refreshAnalyseButton();
  }
}

function loadPayload(data) {
  state.graph = data._graph || { nodes: [], edges: [] };
  state.mergeReport = data._merge_report || [];
  state.meta = { sources: data._sources || [], skipped: data._skipped || [], usage: data._usage || {}, summary: data.diagram_summary || "" };
  // Keep only the entity lists + open questions in the editable payload.
  const payload = { diagram_summary: data.diagram_summary || "", open_questions: (data.open_questions || []).slice() };
  for (const t of TYPE_KEYS) payload[t] = (data[t] || []).map((e) => Object.assign({}, e));
  state.payload = payload;
  state.activeTab = TYPE_KEYS[0];
  $("input-stage").hidden = true;
  $("review-stage").hidden = false;
  $("status").textContent = "";
  renderReview();
}

// ---- review render ----------------------------------------------------------
function renderReview() {
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
  panel.appendChild(el("h2", { text: "Diagram summary" }));
  panel.appendChild(el("p", { text: state.meta.summary || "(no summary)" }));
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
      });
      td.appendChild(input);
      tr.appendChild(td);
    }
    tr.appendChild(
      el("td", null, [
        el("button", {
          class: "row-del",
          title: "Delete row",
          onclick: () => { rows.splice(rowIdx, 1); renderTabs(); renderTabContent(); },
        }, ["×"]),
      ])
    );
    body.appendChild(tr);
  });

  return el("div", { class: "table-wrap" }, [el("table", null, [el("thead", null, [thead]), body])]);
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
  if (!state.mergeReport.length) { panel.appendChild(el("p", { class: "hint", text: "No cross-document merges — each entity came from a single name." })); return; }
  for (const m of state.mergeReport) {
    panel.appendChild(
      el("div", { class: "merge-item", text: `[${m.type}] "${m.canonical}" ← ${m.merged_from.join(", ")}  (sources: ${m.sources})` })
    );
  }
}

// ---- export -----------------------------------------------------------------
async function download(url, opts, fallbackName) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    $("status").className = "error";
    $("status").textContent = detail.detail || `Export failed (${res.status})`;
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
}
function exportBody() {
  return JSON.stringify({ payload: state.payload, graph: state.graph });
}
const JSON_HEADERS = { "Content-Type": "application/json" };

function setupExport() {
  $("export-all").addEventListener("click", () =>
    download("/api/export", { method: "POST", headers: JSON_HEADERS, body: exportBody() }, "eatools_export.zip"));
  $("export-sheet").addEventListener("click", () =>
    download(`/api/export/${state.activeTab === "_graph" ? "applications" : state.activeTab}`,
      { method: "POST", headers: JSON_HEADERS, body: exportBody() }, "sheet.csv"));
  $("export-graph-json").addEventListener("click", () =>
    download("/api/export/graph?format=json", { method: "POST", headers: JSON_HEADERS, body: exportBody() }, "graph.json"));
  $("export-graph-graphml").addEventListener("click", () =>
    download("/api/export/graph?format=graphml", { method: "POST", headers: JSON_HEADERS, body: exportBody() }, "graph.graphml"));
  $("start-over").addEventListener("click", () => {
    state.files = [];
    state.payload = null;
    renderFileList();
    $("review-stage").hidden = true;
    $("input-stage").hidden = false;
    refreshAnalyseButton();
  });
}

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

async function init() {
  setupDropzone();
  setupKeyField();
  setupExport();
  $("analyse").addEventListener("click", analyse);
  try {
    const res = await fetch("/api/health");
    const h = await res.json();
    state.credentials = !!h.credentials;
  } catch (_) {
    state.credentials = false;
  }
  $("key-field").hidden = state.credentials;
  refreshAnalyseButton();
}

init();
