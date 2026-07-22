# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A web app that reads IT architecture diagrams, extracts EA entities via Claude,
and exports LeanIX import CSVs. See `README.md` for the user-facing view.

```bash
# Run (needs ANTHROPIC_API_KEY exported)
python3 -m uvicorn eatools.app:app --port 8100 --reload
```

`.claude/launch.json` defines the same server as `eatools` for the preview tools.

## Architecture

Four modules, each with one job, connected by two plain data shapes:

- `ingest.py` → `SourceDoc` (a normalised diagram: pages of shapes/connectors,
  or images)
- `extract.py` → the entity payload dict (shape defined by `SCHEMA`)
- `leanix.py` consumes that dict and emits CSV
- `app.py` is thin — routing and error mapping only

Adding a new input format means one function in `ingest.py` returning a
`SourceDoc`; nothing downstream changes. Adding an entity type means a schema
block in `extract.py`, a `SHEETS` entry in `leanix.py`, and a `SHEETS` entry in
`frontend/app.js` — those three must stay in agreement.

## Conventions that matter

- **The server is stateless.** The browser holds the extracted payload and posts
  it back to `/api/export`. Don't add server-side session storage without a
  reason — "nothing is retained" is a property worth keeping for a tool people
  point at internal architecture documents.
- **Every extracted entity carries `evidence` and `confidence`.** These are what
  make the output reviewable instead of a black box. If you add a field to an
  entity, don't drop them.
- **The model must not enrich beyond the diagram.** The system prompt in
  `extract.py` says unknown attributes come back `unknown`. Resist changes that
  invite plausible-sounding guesses about lifecycle, criticality, or ownership.
- **`SHEETS` in `leanix.py` is the tenant-adaptation seam.** Keep column
  definitions declarative there rather than scattering formatting logic.
- Static assets are served `no-store` on purpose — the frontend is edited in
  place and a cached `app.js` silently serves stale behaviour.

## Frontend

Plain HTML/CSS/JS, no build step, no dependencies. Built with DOM APIs
(`textContent`, `createElement`) rather than `innerHTML` — extracted content is
model output derived from user files and should never be parsed as markup.

Rebuild the table element on each render; do not cache a reference to a node
that a previous render may have detached.

## Testing

There is no test suite yet. When changing an ingester, verify it against a real
file of that format rather than reasoning about the XML — the fixture-generation
approach in the git history is a reasonable starting point.
