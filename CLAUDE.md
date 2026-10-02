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
- `merge.py` — two steps. `propose()` gives entities stable `_id`s and returns match
  proposals with default decisions. Names compare as keys ignoring case, accents,
  spacing and punctuation, and an Alfabet record matches on its name **without the owner
  suffix** ("(NTTD EMEAL DACH)" is the owning company, not part of the name; a diagram
  that spells the owner out picks that record). Methods are exact / alias / partial
  (word-boundary containment, ≥4 chars, capped per entity) / fuzzy; only partial+fuzzy
  candidates go to the optional Claude verdict. A **company round** then settles an item
  with several equally good records (exact/alias/Claude-same): keep the owners matching
  the Company field, else owner words from the diagram's file name that tell the
  candidates apart; one left → accepted, others rejected; none → unchanged;
  `merge(accepted=[(id, id)])` merges **only reviewer-accepted pairs**, refusing (and
  reporting in `blocked`) any pair that would join two Alfabet records. Alfabet entities
  are never proposed against each other. Union attributes, accumulate evidence +
  provenance, bump confidence on corroboration.
- `leanix.py` — declarative `SHEETS` → CSVs + `relations.csv` + `OPEN_QUESTIONS.txt` +
  graph files → zip.
- `alfabet.py` — Bizzdesign Alfabet EDC workbooks. `read_edc` imports rows as entities
  (no model call) tagged `_alfabet_ref`; `build_edc` fills the **user's own template**
  (Alfabet only re-imports workbooks it generated) with Update/Create rows + a report.
  Picklist values come from the template's reference data; never invent mandatory values.
  In `merge.py`, two different `_alfabet_ref`s are never merged and the Alfabet value
  wins ties. Real EDC templates hold company data — tests use a generated one.
- `alfabet_api.py` — Alfabet REST v2 client (password-grant token, named report queries).
  Credentials only from `ALFABET_URL/USERNAME/PASSWORD` env; never logged or returned.
- `app.py` — routing and error mapping only; no business logic.
- Frontend flow: **Input → Matches → Review → Output.** The browser holds the per-source
  payloads + decisions and posts them to `/api/merge` (server stays stateless). Output
  exports only selected items; `/api/export` and `/api/export/alfabet` require
  `authorisation.by` (recorded in the export), and the EDC write-back writes only the
  plan rows the reviewer authorised. Any change after authorising withdraws it.

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
  **One opt-in exception:** *Save to Keychain* (`keystore.py`) stores a key in the macOS
  Keychain after a verification call, and the app loads it on start. Only from a loopback
  client sending the `X-EATools-Intent` header (forces a CORS preflight the server never
  approves); the key goes to `security` on stdin, never argv; *Forget* removes it.
  Never add another place a key is persisted.
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
