# Rebuild prompt — EATools

Hand the text below to a fresh Claude Code session in an empty repository. It
specifies the application completely enough to reconstruct it, including the
decisions that aren't obvious from a feature list.

---

## The prompt

Build a small web app called **EATools** that reads IT architecture diagrams,
extracts the enterprise-architecture entities they depict using Claude, lets a
human review and correct the result, and exports it as LeanIX import CSVs.

The value is in the review step: a diagram is never unambiguous enough to import
unattended, so the output must be *reviewable* — every extracted entity has to
carry the evidence it came from and a confidence level, and genuine ambiguities
must be surfaced rather than silently resolved.

### Stack & environment

- Python 3 backend, **FastAPI** + **uvicorn**. Homebrew Python; installs use
  `pip3 install --break-system-packages`.
- Frontend is **plain HTML/CSS/JS, no build step, no dependencies**. Build the
  DOM with `textContent`/`createElement`, never `innerHTML` — extracted content
  is model output derived from user files and must never be parsed as markup.
- Anthropic Python SDK. Model `claude-opus-4-8`.
- No database. No test framework required, but parsers must be verified against
  real files of each format (generate small fixtures programmatically).

### Architecture — four modules, two data shapes

Keep the seams clean so each module has one job:

- `model.py` — the two plain dataclasses everything else speaks in:
  - `SourceDoc` — a normalised diagram. Either `kind="structured"` (a list of
    `Page`s, each holding `Shape`s and `Connector`s) or `kind="visual"` (a list
    of base64-encoded PNGs). A `Shape` has id, text, kind, optional `parent`
    (container/grouping), and geometry. A `Connector` has source/target shape
    ids, a label, and direction. `Page.render()` and `SourceDoc.render()`
    flatten the graph to text a model can reason over (list shapes with their
    container nesting, then list connections as `A -> B : label`).
  - The extracted **entity payload** is a plain dict matching the JSON schema
    below.
- `ingest.py` — `ingest(filename, bytes) -> SourceDoc`. Pluggable by suffix.
  **Adding a new input format is one function here and nothing downstream
  changes.** Supported:
  - `.drawio` / `.xml` — parse mxGraphModel. Handle both inline XML and draw.io's
    deflate+base64+URL-encoded payloads. Preserve container nesting (a shape's
    `parent`) because swimlanes/containers are capability hints. Strip HTML from
    labels. Derive a shape `kind` from the mxGraph style string.
  - `.vsdx` — unzip, parse `visio/pages/*.xml`; shapes from `<Shape><Text>`,
    edges from `<Connect>` (FromSheet is the connector shape, ToSheet the
    endpoint; BeginX/EndX distinguish source/target). Drop connector shapes from
    the node list.
  - `.pptx` — python-pptx. Text shapes become nodes; shapes carrying
    `<a:stCxn>/<a:endCxn>` become connectors. Include speaker notes.
  - `.pdf` — PyMuPDF, render pages to PNG at ~150 DPI, cap at 20 pages.
  - images (`.png/.jpg/...`) — normalise to PNG, clamp long edge to ~2576px.
  - Unknown/parse failures raise a typed `UnsupportedFile` with a clear message;
    a bad file must not kill a multi-file batch.
- `extract.py` — one schema-constrained Claude call. Builds a user turn mixing
  rendered graph text (structured docs) and images (visual docs) plus optional
  architect context, then returns the parsed payload dict + token usage. See
  "Extraction" below.
- `leanix.py` — consumes the payload dict and emits CSV. See "Export" below.
- `app.py` — thin: routing and error mapping only.

### Extraction (`extract.py`)

- **JSON schema** constraining the output, with these top-level keys:
  - `diagram_summary` (string).
  - `applications[]`: name, alias, description, `business_criticality`
    (mission_critical | business_critical | business_operational |
    administrative_service | unknown), `lifecycle` (plan | phaseIn | active |
    phaseOut | endOfLife | unknown), `hosting` (cloud | on_premise | hybrid |
    saas | unknown), and name-reference lists `capabilities[]`, `data_objects[]`,
    `it_components[]`, plus `evidence` and `confidence`.
  - `capabilities[]`: name, description, `level` (1|2|3), `parent`, evidence,
    confidence.
  - `it_components[]`: name, description, `category` (software | hardware |
    service | middleware | database | platform | module | unknown), evidence,
    confidence.
  - `data_objects[]`: name, description, `classification` (public | internal |
    confidential | restricted | unknown), evidence, confidence.
  - `interfaces[]`: name, description, `provider`, `consumer`, `data_objects[]`,
    `integration_type` (api | file_transfer | message_queue | database_link |
    etl | manual | unknown), `frequency` (real_time | near_real_time | hourly |
    daily | weekly | monthly | on_demand | unknown), evidence, confidence.
  - `open_questions[]` (strings).
  - Every entity object carries `evidence` (string) and `confidence`
    (high | medium | low). Use `additionalProperties: false`.
- **System prompt** casts the model as an enterprise architect mapping diagram
  content to the five LeanIX fact-sheet types. It must enforce:
  1. Only record what the diagram supports — do **not** invent entities or
     enrich lifecycle/criticality/ownership from product knowledge. Silent
     attributes come back `unknown`/empty, never a guess.
  2. Deduplicate the same system across pages/files into one entity.
  3. Every entity needs `evidence` — the box/connector label or image region.
  4. Set `confidence` honestly (high = explicitly labelled; medium = a reading of
     shape/position/nesting; low = a guess worth reviewing).
  5. Cross-references use exact `name` values so relation import works.
  6. Skip legends, titles, footers, page numbers, decorative shapes.
  7. Put genuine ambiguity in `open_questions` instead of guessing.
- Use streaming, `effort: "high"`, adaptive thinking, and cache the system
  prompt. Map SDK errors (`AuthenticationError`, `PermissionDeniedError`,
  `RateLimitError`, `APIStatusError`, `APIConnectionError`) to short human
  messages and re-raise a typed `ExtractionError` **with `from None`** so request
  internals never reach the browser.

### Export (`leanix.py`)

- A single declarative `SHEETS` structure is **the tenant-adaptation seam**: it
  maps each fact-sheet type to its source key in the payload and a list of
  (column header, value-getter) pairs. LeanIX import templates differ per
  workspace, so keep all column definitions here rather than scattering
  formatting logic. Emit one CSV per fact-sheet type (applications,
  business_capabilities, it_components, data_objects, interfaces), plus a flat
  `relations.csv` edge list, plus `OPEN_QUESTIONS.txt`, bundled as a zip.
- Columns prefixed `_` (e.g. `_evidence`, `_confidence`) are review aids, not
  LeanIX fields. Enum values are shown Title Case; `unknown`/empty render blank.
  Relation columns join name lists with `; `. Write UTF-8 with BOM so Excel opens
  cleanly.
- **Adding an entity type means three changes that must stay in agreement:** a
  schema block in `extract.py`, a `SHEETS` entry in `leanix.py`, and a `SHEETS`
  entry in `frontend/app.js`.

### API (`app.py`)

- `GET /api/health` → `{ok, credentials}` where `credentials` reflects whether
  the server has an API key of its own.
- `POST /api/analyse` — multipart: files[] + optional `context` form field.
  Ingest each file (skip+report unreadable ones, enforce a total upload cap),
  run extraction, return the payload plus `_sources` and `_skipped` metadata.
- `POST /api/export` — takes the (edited) payload JSON, returns the zip.
- `POST /api/export/{sheet}` — returns a single CSV.
- `GET /` serves the frontend; static assets served `no-store` (the frontend is
  edited in place; a cached `app.js` silently serves stale behaviour).

### API key handling — deliberate, not incidental

The server works two ways: if `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) is
in the environment, it uses that and the UI hides the key field. Otherwise the UI
shows a key field. When supplied from the browser, the key:

- travels as an **`X-Anthropic-Api-Key` header**, never a form field or query
  param (headers stay out of uvicorn's access log and browser history);
- is used to build a client for that one request and is **never persisted,
  logged, or echoed back** in any response;
- lives in the browser's **`sessionStorage`, not `localStorage`** (dies with the
  tab);
- is **masked** by default with explicit Show and Clear controls.

If you touch this path: don't log request headers, don't add the key to any
response, don't move it to a query parameter. Document the loopback/plain-HTTP
caveat — the in-app field must not be used if the server is ever bound beyond
localhost; use the env var there.

### Statelessness

The server keeps nothing. The browser holds the extracted payload and posts it
back to `/api/export`. No diagram is written to disk or retained after the
response. Don't add server-side session storage without a reason — "nothing is
retained" is a property worth keeping for a tool people point at internal
architecture documents.

### Frontend

Single page: a drag-and-drop upload zone (accepting the supported extensions), an
optional "context" textarea, the API-key field (shown only when the server has no
key), and an **Analyse** button gated on having both files and credentials. After
analysis, render a tabbed review table — one tab per entity type with counts —
where **every cell is an editable input** and rows can be deleted; list-valued
fields edit as `; `-separated text. Show the diagram summary, source/skipped/token
metadata, and an open-questions panel. Export buttons: all CSVs (zip), this sheet
(CSV), and start over. Rebuild the table element on each render; never cache a
detached node. Theme-aware (light/dark via `prefers-color-scheme`).

### Deliverables

`eatools/` package (`model.py`, `ingest.py`, `extract.py`, `leanix.py`,
`app.py`), `frontend/` (`index.html`, `style.css`, `app.js`), `requirements.txt`,
a `README.md`, a `CLAUDE.md` capturing the conventions above, and a
`.claude/launch.json` defining the uvicorn server as `eatools` on port 8100.
Verify each ingester against a generated sample file of that format, and verify
one real end-to-end extraction+export before declaring done.
```
