"""FastAPI app: routing and error mapping only. All real work lives in the other modules.

The server is **stateless** — no diagram bytes are written to disk or retained after a
response. The browser holds the extracted payload + graph and posts them back to
``/api/export``.

API-key handling is deliberate: a browser-supplied key arrives in the
``X-Anthropic-Api-Key`` header (kept out of access logs and browser history), is used to
build a one-shot client, and is never persisted, logged, or echoed. If the server has its
own ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` in the environment, that is used and
the UI hides its key field. The in-app key field must not be used when the server is
bound beyond localhost over plain HTTP — set the env var there instead (see README).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from . import alfabet, alfabet_api, keystore, leanix
from .extract import ExtractionError, extract
from .ingest import UnsupportedFile, ingest
from .merge import merge, propose_and_merge

MAX_TOTAL_UPLOAD = 100 * 1024 * 1024  # 100 MB across the whole batch
MAX_FILES = 50
MAX_EXTRACT_WORKERS = 8

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
_NO_STORE = {"Cache-Control": "no-store"}

app = FastAPI(title="EATools")

# A key the user chose to save (keystore.py) survives restarts; an env key still wins.
keystore.load_into_env()

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_INTENT_HEADER = "x-eatools-intent"


def _require_local_intent(request: Request, intent: str) -> None:
    """Key persistence is only for the user at this machine, via this app's own page.

    The custom header forces a CORS preflight, which this server never approves, so a
    third-party page cannot make the browser save (and so hijack) a key here.
    """
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK or request.headers.get(_INTENT_HEADER) != intent:
        raise HTTPException(status_code=403, detail="Key storage is only available from this computer.")


def _server_has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _build_client(header_key: str | None) -> anthropic.Anthropic:
    """One-shot client. Header key wins; otherwise the SDK reads the environment."""
    if header_key and header_key.strip():
        return anthropic.Anthropic(api_key=header_key.strip())
    if _server_has_key():
        return anthropic.Anthropic()
    raise HTTPException(status_code=400, detail="No API key: provide one or set ANTHROPIC_API_KEY.")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "credentials": _server_has_key(),
        "saved_key": keystore.is_saved_key_active(),
        "keychain": keystore.available(),
        "alfabet": alfabet_api.is_configured(),
    }


@app.post("/api/key")
def save_key(request: Request, body: dict):
    _require_local_intent(request, "save-key")
    key = str(body.get("key", "")).strip()
    try:
        if not keystore.valid_format(key):
            raise keystore.KeystoreError("That does not look like an Anthropic API key (sk-ant-...).")
        keystore.verify(key)
        keystore.save(key)
    except keystore.KeystoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True}


@app.delete("/api/key")
def forget_key(request: Request):
    _require_local_intent(request, "forget-key")
    try:
        keystore.forget()
    except keystore.KeystoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "credentials": _server_has_key()}


@app.post("/api/analyse")
def analyse(
    files: list[UploadFile] = File(default=[]),
    context: str = Form(""),
    company: str = Form(""),
    alfabet_import: bool = Form(False),
    alfabet_company: str = Form(""),
    alfabet_name: str = Form(""),
    alfabet_version: str = Form(""),
    alfabet_objectstate: str = Form(""),
    x_anthropic_api_key: str | None = Header(default=None),
):
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"Too many files (max {MAX_FILES}).")

    sources = []
    imported = []  # (filename, payload) from Alfabet EDC workbooks -- no model call
    skipped = []
    total = 0
    for upload in files:
        name = upload.filename or "unnamed"
        data = upload.file.read()
        total += len(data)
        if total > MAX_TOTAL_UPLOAD:
            raise HTTPException(status_code=413, detail="Upload exceeds the total size cap.")
        try:
            if alfabet.is_edc_filename(name):
                imported.append((name, alfabet.read_edc(name, data)))
            else:
                sources.append(ingest(name, data))
        except UnsupportedFile as exc:
            skipped.append({"name": name, "reason": str(exc)})

    if alfabet_import:
        try:
            objects, args = alfabet_api.fetch_selection(
                alfabet_company, alfabet_name, alfabet_version, alfabet_objectstate
            )
        except alfabet_api.AlfabetApiError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        label = "Alfabet API" + (f" ({', '.join(f'{k}={v}' for k, v in args.items())})" if args else " (all)")
        if objects:
            imported.append((label, alfabet.payload_from_api(objects, label)))
        else:
            skipped.append({"name": label, "reason": "no applications matched the selection"})

    if not sources and not imported:
        reasons = "; ".join(f"{s['name']}: {s['reason']}" for s in skipped)
        raise HTTPException(
            status_code=400,
            detail=f"Nothing to analyse -- {reasons}" if reasons else "No readable diagrams or Alfabet input.",
        )

    # An Alfabet-only import needs no key; with no candidates to reconcile, merge makes no call.
    client = _build_client(x_anthropic_api_key) if sources or x_anthropic_api_key or _server_has_key() else None

    # Per-document extraction, run concurrently. A single doc's failure aborts the batch
    # with its user-safe message; ingest failures were already collected above.
    def run(src):
        return extract(src, context, client)

    results = []
    if sources:
        try:
            with ThreadPoolExecutor(max_workers=min(MAX_EXTRACT_WORKERS, len(sources))) as pool:
                results = list(pool.map(run, sources))
        except ExtractionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    per_doc_payloads = [payload for _, payload in imported] + [payload for payload, _ in results]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    for _, u in results:
        for key in usage:
            usage[key] += u.get(key, 0)

    merged = propose_and_merge(per_doc_payloads, client=client, company=company or alfabet_company)

    response = dict(merged["payload"])
    response["_graph"] = merged["graph"]
    response["_merge_report"] = merged["merge_report"]
    # For the Matches tab: the unmerged per-source entities (with ids), the proposed
    # matches and their default decisions. The browser posts these back to /api/merge.
    response["_source_payloads"] = per_doc_payloads
    response["_proposals"] = merged["proposals"]
    response["_match_notes"] = merged["notes"]
    response["_blocked"] = merged["blocked"]
    response["_sources"] = [
        {"name": name, "kind": "alfabet", "pages": 0, "images": 0} for name, _ in imported
    ] + [
        {"name": s.source_name, "kind": s.kind, "pages": len(s.pages), "images": len(s.images)}
        for s in sources
    ]
    response["_skipped"] = skipped
    response["_usage"] = usage
    return JSONResponse(response)


@app.post("/api/merge")
def merge_decisions(body: dict):
    """Re-merge the per-source entities using the reviewer's accepted match pairs."""
    sources = body.get("sources")
    accepted = body.get("accepted")
    if not isinstance(sources, list) or not isinstance(accepted, list):
        raise HTTPException(status_code=400, detail="Expected 'sources' and 'accepted' lists.")
    merged = merge([s for s in sources if isinstance(s, dict)], accepted=accepted)
    response = dict(merged["payload"])
    response["_graph"] = merged["graph"]
    response["_merge_report"] = merged["merge_report"]
    response["_blocked"] = merged["blocked"]
    return JSONResponse(response)


def _authorisation(body: dict) -> dict:
    """Exports that leave EATools must name who authorised them; the server stamps the time."""
    auth = body.get("authorisation") if isinstance(body, dict) else None
    by = str((auth or {}).get("by", "")).strip()[:100]
    if not by:
        raise HTTPException(status_code=400, detail="Export needs an authorisation (Authorised by).")
    return {"by": by, "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


@app.post("/api/export")
def export_zip(body: dict):
    authorisation = _authorisation(body)
    payload = body.get("payload", body)
    graph = body.get("graph") or payload.get("_graph") or {"nodes": [], "edges": []}
    data = leanix.build_zip(payload, graph, authorisation)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="eatools_export.zip"'},
    )


@app.post("/api/export/graph")
def export_graph(body: dict, format: str = "json"):
    graph = body.get("graph") or body.get("payload", {}).get("_graph") or {"nodes": [], "edges": []}
    if format == "graphml":
        return Response(
            content=leanix.graph_graphml(graph),
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="graph.graphml"'},
        )
    return Response(
        content=leanix.graph_json(graph),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="graph.json"'},
    )


def _template_and_body(template: UploadFile, body: UploadFile) -> tuple[bytes, dict]:
    """The reviewed payload arrives as a file part, not a form field: a full Alfabet import
    is several MB of JSON and Starlette caps plain form fields at 1 MB."""
    data = template.file.read(MAX_TOTAL_UPLOAD + 1)
    raw = body.file.read(MAX_TOTAL_UPLOAD + 1)
    if len(data) > MAX_TOTAL_UPLOAD or len(raw) > MAX_TOTAL_UPLOAD:
        raise HTTPException(status_code=413, detail="Upload exceeds the size cap.")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed export body.") from None
    return data, parsed if isinstance(parsed, dict) else {}


@app.post("/api/alfabet/plan")
def plan_alfabet(template: UploadFile = File(...), body: UploadFile = File(...)):
    """Preview the EDC write-back so the reviewer can authorise it row by row."""
    data, parsed = _template_and_body(template, body)
    try:
        plan = alfabet.plan_edc(template.filename or "alfabet.xlsx", data, parsed.get("payload", parsed))
    except UnsupportedFile as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(plan)


@app.post("/api/export/alfabet")
def export_alfabet(template: UploadFile = File(...), body: UploadFile = File(...)):
    """Fill the user's own EDC template; Alfabet only re-imports workbooks it generated."""
    data, parsed = _template_and_body(template, body)
    authorisation = _authorisation(parsed)
    authorised_ids = parsed.get("authorised_ids")
    if authorised_ids is not None and not isinstance(authorised_ids, list):
        raise HTTPException(status_code=400, detail="'authorised_ids' must be a list.")
    try:
        result = alfabet.build_edc(
            template.filename or "alfabet.xlsx",
            data,
            parsed.get("payload", parsed),
            authorised_ids=[str(i) for i in authorised_ids] if authorised_ids is not None else None,
            authorisation=authorisation,
        )
    except UnsupportedFile as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return Response(
        content=result,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="alfabet_export.zip"'},
    )


@app.post("/api/export/{sheet}")
def export_sheet(sheet: str, body: dict):
    payload = body.get("payload", body)
    try:
        data = leanix.sheet_csv(sheet, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown sheet '{sheet}'.") from None
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{leanix.SHEETS[sheet]["filename"]}"'},
    )


@app.get("/")
def index():
    return FileResponse(_FRONTEND / "index.html", headers=_NO_STORE)


@app.get("/{asset}")
def static_asset(asset: str):
    # Frontend is edited in place; serve no-store so a cached app.js never masks a change.
    if asset in ("app.js", "style.css"):
        media = "application/javascript" if asset.endswith(".js") else "text/css"
        return FileResponse(_FRONTEND / asset, media_type=media, headers=_NO_STORE)
    raise HTTPException(status_code=404, detail="Not found")
