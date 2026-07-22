# EATools

Reads IT architecture diagrams, extracts the applications, capabilities, IT
components, data objects and integrations they depict, and exports them as
LeanIX import CSVs — with a review step in between, because a diagram is never
unambiguous enough to import unattended.

## Run it

```bash
pip3 install --break-system-packages -r requirements.txt
python3 -m uvicorn eatools.app:app --port 8100 --reload
```

Then open <http://localhost:8100>.

## Supplying an API key

Two options — the app works either way.

**Environment (preferred).** Start the server with `ANTHROPIC_API_KEY` set and
the key never touches the browser; the key field hides itself:

```bash
ANTHROPIC_API_KEY=sk-ant-... python3 -m uvicorn eatools.app:app --port 8100
```

**In the app.** If the server has no key, a field appears. What happens to it:

- Kept in `sessionStorage`, so it dies with the browser tab — not `localStorage`
- Sent as an `X-Anthropic-Api-Key` header, not a form field or query string, so
  it stays out of uvicorn's access log and out of browser history
- Used to construct a client for that one request; never written to disk,
  never logged, never returned in a response
- Masked by default, with explicit Show and Clear controls

The caveat worth knowing: this is plain HTTP on loopback, which is fine on your
own machine. **Don't bind this server to `0.0.0.0` or put it behind a tunnel**
while using the in-app key field — the key would cross the network in clear text.
For anything beyond localhost, use the environment variable instead.

## How it works

```
diagram files ──▶ ingest ──▶ normalised graph ──▶ Claude ──▶ review UI ──▶ LeanIX CSVs
```

**Ingestion** (`eatools/ingest.py`) takes whatever people actually send you:

| Format | Handling |
|---|---|
| `.drawio`, `.xml` | Parsed to a real shape/connector graph. Handles both plain and deflate-compressed `<diagram>` payloads. Container nesting is preserved, so swimlanes and zones become capability hints. |
| `.vsdx` (Visio, Lucid export) | Shapes plus `Connects` records resolved into directed edges. |
| `.pptx` | Shapes, speaker notes, and connectors resolved via `stCxn`/`endCxn` to their endpoint shapes. |
| `.pdf` | Pages rendered to PNG (first 20) for the vision model. |
| Images | Normalised to PNG, long edge clamped to 2576px. |

Structured formats become text the model reads as a graph; images go to vision.
Both land in the same `SourceDoc`, so a single run can mix a Visio file, a deck,
and a screenshot.

**Extraction** (`eatools/extract.py`) is one Claude call with a JSON schema, so
the output shape is guaranteed. The system prompt is cached. Every entity carries:

- `evidence` — the box or connector label it came from, so a reviewer can check it
- `confidence` — `high` (explicitly labelled), `medium` (inferred from shape or
  nesting), `low` (a guess)

`open_questions` collects genuine ambiguities rather than letting the model
resolve them silently.

**Review** — the browser holds the result and every cell is editable. Nothing is
written to disk and no diagram is retained after the response is sent.

**Export** (`eatools/leanix.py`) writes one CSV per fact sheet type plus a flat
`relations.csv` edge list, bundled as a zip.

## Adapting the LeanIX columns

LeanIX import templates differ per tenant — custom fields, renamed sections,
different relation column names. `SHEETS` in `eatools/leanix.py` is the single
place to change:

```python
"applications": ("applications", [
    ("type", lambda r: "Application"),
    ("name", _f("name")),
    ("businessCriticality", _enum("business_criticality")),
    ("relApplicationToBusinessCapability", _list("capabilities")),
    ...
]),
```

Rename a header string, reorder, or drop a row and the export follows. The
column names shipped here follow LeanIX's standard fact sheet fields and
`rel<From>To<To>` relation convention — **check them against your own workspace's
import template before a bulk load.** Columns prefixed `_` are review aids, not
LeanIX fields; LeanIX ignores unrecognised columns, so you can leave them.

## Expectations

The extraction is a first pass, not an authority. It is good at reading labelled
boxes and connectors, and reasonable at inferring capability hierarchy from
nesting. It cannot know lifecycle, criticality, or ownership unless the diagram
says so — those come back `unknown` by design rather than as plausible guesses.
Review the `low` confidence rows and the open questions before importing.

## Layout

```
eatools/
  ingest.py    file -> normalised diagram graph
  model.py     Shape / Connector / Page / SourceDoc
  extract.py   Claude call + JSON schema + the extraction prompt
  leanix.py    entities -> CSV (edit SHEETS here)
  app.py       FastAPI routes
frontend/      single page: upload, review table, export
```
