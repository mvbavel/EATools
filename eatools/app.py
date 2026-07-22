"""FastAPI server: upload diagrams, review the extraction, export LeanIX CSVs.

Deliberately stateless -- the browser holds the extracted payload and posts it
back to /api/export. Nothing is written to disk and no diagram is retained
after the response is sent.
"""

from __future__ import annotations

import os
from pathlib import Path

from typing import Annotated

from fastapi import FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import leanix
from .extract import ExtractionError, extract
from .ingest import UnsupportedFile, ingest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD_BYTES = 40 * 1024 * 1024

app = FastAPI(title="EATools", docs_url=None, redoc_url=None)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "credentials": bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
    }


@app.post("/api/analyse")
async def analyse(
    files: list[UploadFile],
    context: str = Form(""),
    # Carried as a header, not a form field or query param: headers stay out of
    # uvicorn's access log and out of browser history. Used for this request
    # only -- never stored, never returned.
    x_anthropic_api_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    if not files:
        raise HTTPException(400, "No files uploaded.")

    docs, skipped = [], []
    total = 0

    for upload in files:
        data = await upload.read()
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
                "Try fewer diagrams per run.",
            )
        try:
            docs.append(ingest(upload.filename or "unnamed", data))
        except UnsupportedFile as exc:
            skipped.append(str(exc))
        except Exception as exc:  # a malformed file shouldn't kill the batch
            skipped.append(f"{upload.filename}: could not parse ({exc})")

    if not docs:
        raise HTTPException(400, "No readable diagrams. " + " ".join(skipped))

    try:
        payload = extract(docs, context, api_key=(x_anthropic_api_key or "").strip() or None)
    except ExtractionError as exc:
        raise HTTPException(502, str(exc)) from exc

    payload["_sources"] = [
        {"filename": d.filename, "kind": d.kind,
         "pages": len(d.pages), "images": len(d.images)}
        for d in docs
    ]
    payload["_skipped"] = skipped
    return JSONResponse(payload)


@app.post("/api/export")
async def export(payload: dict) -> Response:
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object.")
    return Response(
        content=leanix.bundle_zip(payload),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="leanix-import.zip"'},
    )


@app.post("/api/export/{sheet}")
async def export_sheet(sheet: str, payload: dict) -> Response:
    if sheet == "relations":
        content = leanix.relations_csv(payload)
    elif sheet in leanix.SHEETS:
        content = leanix.sheet_csv(sheet, payload)
    else:
        raise HTTPException(404, f"Unknown sheet '{sheet}'.")
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{sheet}.csv"'},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html", headers=NO_STORE)


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


NO_STORE = {"Cache-Control": "no-store"}


@app.middleware("http")
async def no_cache_assets(request, call_next):
    """This runs locally and the frontend is edited in place -- a cached
    app.js silently serves stale behaviour, which is worse than a re-fetch."""
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response
