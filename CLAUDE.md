# EATools — conventions

EATools reads IT architecture diagrams, extracts enterprise-architecture entities with
Claude, merges them across many documents into one picture, lets a human review/correct
the result, and exports LeanIX import CSVs + the merged graph.

## Architecture — keep the seams clean

Four backend jobs, each in one module; two data shapes everything speaks in.

- `model.py` — `SourceDoc` (a normalised diagram: `structured` graph or `visual` PNGs)
  and the entity **payload dict** (matches `schema.EXTRACTION_SCHEMA`).
- `ingest.py` — `ingest(filename, bytes) -> SourceDoc`, dispatched by suffix. **Adding an
  input format is one function here plus one `_PARSERS` line — nothing downstream changes.**
  Parse failures raise `UnsupportedFile`; a bad file must never kill a multi-file batch.
- `extract.py` — one schema-constrained Claude call **per document**. Model
  `claude-opus-4-8`, streaming, adaptive thinking, `effort: high`, cached system prompt.
- `merge.py` — builds the in-memory graph and does hybrid cross-document resolution
  (deterministic name/alias grouping + Claude reconciliation of near-duplicates). Union
  attributes, accumulate evidence + provenance, bump confidence on corroboration.
- `leanix.py` — declarative `SHEETS` → CSVs + `relations.csv` + `OPEN_QUESTIONS.txt` +
  graph files → zip.
- `app.py` — routing and error mapping only; no business logic.

## Non-negotiable rules

- **Frontend: no build step, no dependencies.** Build DOM with
  `textContent`/`createElement`/`createElementNS` — **never `innerHTML`**. Extracted
  content is model output derived from user files and must never be parsed as markup.
  Rebuild the table/graph elements on each render; never cache a detached node.
- **Stateless server.** Nothing is written to disk or retained after a response; the merge
  graph lives only for one request. Don't add server-side session storage.
- **API key path.** A browser key travels only in the `X-Anthropic-Api-Key` header; never
  log request headers, never add the key to any response, never move it to a query param.
  It lives in `sessionStorage`, masked, with Show/Clear. Document the plain-HTTP caveat.
- **Extraction honesty.** The system prompt forbids inventing entities or enriching
  attributes from product knowledge; silent attributes are `unknown`/empty. Every entity
  carries `evidence` + `confidence`; ambiguity goes to `open_questions`. Cross-references
  use exact `name` values.
- **Error mapping.** Map SDK errors to short human messages and re-raise `ExtractionError`
  **with `from None`** so request internals never reach the browser.
- **CSV output.** Columns prefixed `_` are review aids, not LeanIX fields. Enums render
  Title Case; `unknown`/empty render blank; list columns join with `; `; UTF-8 **with BOM**.

## The three-in-agreement rule

Adding an entity type means three changes that must stay in agreement:
a schema block in `extract.py`/`schema.py`, a `SHEETS` entry in `leanix.py`, and a
`SHEETS` entry in `frontend/app.js`.

## Environment

Homebrew Python 3; installs use `pip3 install --break-system-packages`. Run with
`uvicorn eatools.app:app --port 8100`. Parsers are verified against generated sample files
of each format (see `tests/`); verify one real end-to-end extraction+export before
declaring a change done.
