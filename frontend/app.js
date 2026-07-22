'use strict';

// Column layout per entity type. `list: true` means the value is an array of
// names and is edited as a semicolon-separated string.
const SHEETS = {
  applications: {
    label: 'Applications',
    columns: [
      { key: 'name' }, { key: 'alias' }, { key: 'description' },
      { key: 'business_criticality', label: 'criticality' },
      { key: 'lifecycle' }, { key: 'hosting' },
      { key: 'capabilities', list: true },
      { key: 'data_objects', list: true },
      { key: 'it_components', list: true },
      { key: 'evidence' }, { key: 'confidence' },
    ],
  },
  capabilities: {
    label: 'Capabilities',
    columns: [
      { key: 'name' }, { key: 'description' }, { key: 'level' },
      { key: 'parent' }, { key: 'evidence' }, { key: 'confidence' },
    ],
  },
  it_components: {
    label: 'IT Components',
    columns: [
      { key: 'name' }, { key: 'description' }, { key: 'category' },
      { key: 'evidence' }, { key: 'confidence' },
    ],
  },
  data_objects: {
    label: 'Data Objects',
    columns: [
      { key: 'name' }, { key: 'description' }, { key: 'classification' },
      { key: 'evidence' }, { key: 'confidence' },
    ],
  },
  interfaces: {
    label: 'Integrations',
    columns: [
      { key: 'name' }, { key: 'description' },
      { key: 'provider' }, { key: 'consumer' },
      { key: 'data_objects', list: true },
      { key: 'integration_type', label: 'type' }, { key: 'frequency' },
      { key: 'evidence' }, { key: 'confidence' },
    ],
  },
};

// CSV filename per sheet — must match the server's route names.
const CSV_NAME = {
  applications: 'applications',
  capabilities: 'business_capabilities',
  it_components: 'it_components',
  data_objects: 'data_objects',
  interfaces: 'interfaces',
};

const $ = (sel) => document.querySelector(sel);

let files = [];
let payload = null;
let activeSheet = 'applications';

// ---------------------------------------------------------------- uploading

const dropzone = $('#dropzone');
const fileInput = $('#file-input');

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
['dragenter', 'dragover'].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add('over');
  }));
['dragleave', 'drop'].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove('over');
  }));

dropzone.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));
fileInput.addEventListener('change', () => {
  addFiles(fileInput.files);
  fileInput.value = '';
});

function addFiles(list) {
  for (const f of list) {
    if (!files.some((x) => x.name === f.name && x.size === f.size)) files.push(f);
  }
  renderFiles();
}

function renderFiles() {
  const ul = $('#file-list');
  ul.textContent = '';
  files.forEach((f, i) => {
    const li = document.createElement('li');
    const span = document.createElement('span');
    span.textContent = `${f.name} — ${(f.size / 1024).toFixed(0)} KB`;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '✕';
    btn.title = `Remove ${f.name}`;
    btn.addEventListener('click', () => { files.splice(i, 1); renderFiles(); });
    li.append(span, btn);
    ul.append(li);
  });
  $('#analyse').disabled = files.length === 0;
}

// ---------------------------------------------------------------- analysing

function setStatus(text, kind) {
  const el = $('#status');
  el.textContent = text;
  el.className = kind || '';
}

$('#analyse').addEventListener('click', async () => {
  const body = new FormData();
  files.forEach((f) => body.append('files', f));
  body.append('context', $('#context').value);

  $('#analyse').disabled = true;
  setStatus('Reading diagrams — this can take a minute or two', 'busy');

  try {
    const res = await fetch('/api/analyse', { method: 'POST', body });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }
    payload = await res.json();
    setStatus('');
    showResults();
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    $('#analyse').disabled = files.length === 0;
  }
});

// ----------------------------------------------------------------- results

function showResults() {
  $('#upload-panel').hidden = true;
  $('#results').hidden = false;

  const summary = $('#summary');
  summary.textContent = '';

  if (payload.diagram_summary) {
    const p = document.createElement('p');
    p.textContent = payload.diagram_summary;
    summary.append(p);
  }

  const bits = [];
  const sources = payload._sources || [];
  if (sources.length) bits.push(`${sources.length} file(s): ${sources.map((s) => s.filename).join(', ')}`);
  const u = payload._usage;
  if (u) bits.push(`${u.input_tokens.toLocaleString()} in / ${u.output_tokens.toLocaleString()} out tokens`);
  (payload._skipped || []).forEach((s) => bits.push(`skipped — ${s}`));

  if (bits.length) {
    const meta = document.createElement('p');
    meta.className = 'meta';
    meta.textContent = bits.join(' · ');
    summary.append(meta);
  }

  renderQuestions();
  renderTabs();
  renderTable();
}

function renderQuestions() {
  const box = $('#questions');
  box.textContent = '';
  const qs = payload.open_questions || [];
  if (!qs.length) return;

  const h = document.createElement('h3');
  h.textContent = 'Open questions for review';
  const ul = document.createElement('ul');
  qs.forEach((q) => {
    const li = document.createElement('li');
    li.textContent = q;
    ul.append(li);
  });
  box.append(h, ul);
}

function renderTabs() {
  const nav = $('#tabs');
  nav.textContent = '';
  for (const [key, spec] of Object.entries(SHEETS)) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', String(key === activeSheet));
    btn.append(document.createTextNode(spec.label));

    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = String((payload[key] || []).length);
    btn.append(count);

    btn.addEventListener('click', () => {
      activeSheet = key;
      renderTabs();
      renderTable();
    });
    nav.append(btn);
  }
}

function renderTable() {
  const spec = SHEETS[activeSheet];
  const rows = payload[activeSheet] || [];
  const wrap = $('#table-wrap');

  if (!rows.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = `No ${spec.label.toLowerCase()} found in these diagrams.`;
    wrap.replaceChildren(p);
    return;
  }

  // Build a fresh table every time — reusing a detached node silently breaks
  // the next render.
  const table = document.createElement('table');
  wrap.replaceChildren(table);

  const thead = table.createTHead().insertRow();
  spec.columns.forEach((col) => {
    const th = document.createElement('th');
    th.textContent = (col.label || col.key).replace(/_/g, ' ');
    thead.append(th);
  });
  thead.append(document.createElement('th'));

  const tbody = table.createTBody();
  rows.forEach((row, index) => {
    const tr = tbody.insertRow();
    spec.columns.forEach((col) => {
      const td = tr.insertCell();
      const value = row[col.key];

      const input = document.createElement('input');
      input.type = 'text';
      input.value = col.list ? (value || []).join('; ') : (value ?? '');
      input.addEventListener('change', () => {
        row[col.key] = col.list
          ? input.value.split(';').map((s) => s.trim()).filter(Boolean)
          : input.value;
      });
      td.append(input);

      if (col.key === 'confidence' && value) td.className = `conf-${value}`;
    });

    const last = tr.insertCell();
    const drop = document.createElement('button');
    drop.type = 'button';
    drop.className = 'row-drop';
    drop.textContent = '✕';
    drop.title = 'Remove this row';
    drop.addEventListener('click', () => {
      rows.splice(index, 1);
      renderTabs();
      renderTable();
    });
    last.append(drop);
  });
}

// ----------------------------------------------------------------- exporting

function cleanPayload() {
  const out = {};
  for (const key of Object.keys(SHEETS)) out[key] = payload[key] || [];
  out.open_questions = payload.open_questions || [];
  return out;
}

async function download(url, fallbackName) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cleanPayload()),
  });
  if (!res.ok) {
    setStatus(`Export failed (${res.status})`, 'error');
    return;
  }
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = href;
  a.download = fallbackName;
  a.click();
  URL.revokeObjectURL(href);
}

$('#export-zip').addEventListener('click', () =>
  download('/api/export', 'leanix-import.zip'));

$('#export-sheet').addEventListener('click', () => {
  const name = CSV_NAME[activeSheet];
  download(`/api/export/${name}`, `${name}.csv`);
});

$('#restart').addEventListener('click', () => {
  files = [];
  payload = null;
  activeSheet = 'applications';
  renderFiles();
  $('#context').value = '';
  $('#results').hidden = true;
  $('#upload-panel').hidden = false;
  setStatus('');
});

// --------------------------------------------------------------- startup

fetch('/api/health')
  .then((r) => r.json())
  .then((h) => {
    if (!h.credentials) {
      setStatus('ANTHROPIC_API_KEY is not set — restart the server with it exported.', 'error');
    }
  })
  .catch(() => {});
