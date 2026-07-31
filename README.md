# EATools

Read IT architecture diagrams, extract the enterprise-architecture entities they depict
using Claude, review and correct the result in the browser, and export it as LeanIX import
CSVs. Upload **many diagrams of the same landscape at once** and EATools merges what they
depict into one consolidated picture.

The value is the review step: a diagram is never unambiguous enough to import unattended,
so every extracted entity carries the **evidence** it came from, a **confidence** level,
and its **provenance** (which document(s) it came from). Genuine ambiguities and every
cross-document merge are surfaced for a human, not silently resolved.

## What it does

1. **Ingest** — normalises each uploaded file into a common shape. Structured formats
   (draw.io, Visio, PowerPoint) become a graph of shapes + connectors; page formats (PDF,
   images) become PNGs for a vision model.
2. **Extract** — one schema-constrained Claude call per document (`claude-opus-4-8`),
   producing Applications, Business Capabilities, IT Components, Data Objects, and
   Interfaces, each with evidence and confidence.
3. **Merge** — builds an in-memory graph and consolidates entities across all documents.
   Exact/alias name matches merge deterministically; near-duplicates ("SAP" vs "SAP ERP")
   are resolved by a second Claude reconciliation pass. Attributes are unioned, evidence
   accumulated, confidence bumped when independent documents agree.
4. **Review** — a tabbed, fully editable table (one tab per entity type), a graph view
   with provenance, open questions, and a merge report.
5. **Export** — per-type LeanIX CSVs + a flat `relations.csv` + `OPEN_QUESTIONS.txt` +
   `graph.json` / `graph.graphml`, bundled as a zip.

## Supported inputs

`.drawio` `.xml` (mxGraph, inline or compressed) · `.vsdx` · `.pptx` · `.pdf` ·
`.png .jpg .jpeg .gif .webp .bmp .tiff`

## Setup

Homebrew Python 3. Install dependencies:

```sh
pip3 install --break-system-packages -r requirements.txt
```

## Run

```sh
python3 -m uvicorn eatools.app:app --host 127.0.0.1 --port 8100 --reload
```

Then open <http://127.0.0.1:8100>. (A `.claude/launch.json` defines the same server as
`eatools` on port 8100.)

## API key

The server works two ways:

* **`ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) in the environment** — the server uses
  it and the UI hides the key field. Recommended.
* **No env key** — the UI shows a key field. The key travels in an `X-Anthropic-Api-Key`
  header, is used to build a one-shot client, is never persisted/logged/echoed, and lives
  only in the browser tab's `sessionStorage`.

⚠️ **Loopback / plain-HTTP caveat:** the in-app key field must **not** be used if the
server is ever bound beyond `localhost` over plain HTTP — the key would cross the network
in the clear. Use the environment variable in that case.

## Statelessness

The server keeps nothing: no diagram is written to disk or retained after a response, and
the merge graph lives only for the duration of one request. The browser holds the
extracted payload and posts it back to `/api/export`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/api/health` | `{ok, credentials}` — `credentials` = server has its own key |
| POST | `/api/analyse` | multipart `files[]` + optional `context` → payload + `_graph` + `_merge_report` + `_sources` + `_skipped` + `_usage` |
| POST | `/api/export` | payload+graph JSON → zip of all CSVs + graph files |
| POST | `/api/export/{sheet}` | one fact-sheet CSV |
| POST | `/api/export/graph?format=json\|graphml` | graph file |

## Module map

| File | Job |
| --- | --- |
| `eatools/model.py` | `SourceDoc`/`Page`/`Shape`/`Connector` dataclasses + `render()` |
| `eatools/ingest.py` | `ingest(filename, bytes) -> SourceDoc`, pluggable by suffix |
| `eatools/schema.py` | extraction JSON schema + enum vocabularies |
| `eatools/extract.py` | one schema-constrained Claude call per document |
| `eatools/merge.py` | in-memory graph + hybrid cross-document entity resolution |
| `eatools/leanix.py` | declarative `SHEETS` → CSVs + relations + graph export → zip |
| `eatools/app.py` | FastAPI routing + error mapping only |
| `frontend/` | single-page review UI (no build step, no dependencies) |
