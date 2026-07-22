"""Pluggable ingestion: any diagram file -> SourceDoc.

Structured formats (draw.io, Visio, PowerPoint) are parsed into a real
shape/connector graph. Images and PDFs are rendered to PNGs for the vision
model. Everything lands in the same SourceDoc so extraction doesn't care.
"""

from __future__ import annotations

import base64
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .model import Connector, Page, Shape, SourceDoc

MAX_IMAGE_EDGE = 2576  # Opus high-res vision ceiling
PDF_RENDER_DPI = 150

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


class UnsupportedFile(Exception):
    pass


def ingest(filename: str, data: bytes) -> SourceDoc:
    suffix = Path(filename).suffix.lower()

    if suffix in (".drawio", ".xml"):
        return _ingest_drawio(filename, data)
    if suffix == ".vsdx":
        return _ingest_vsdx(filename, data)
    if suffix == ".pptx":
        return _ingest_pptx(filename, data)
    if suffix == ".pdf":
        return _ingest_pdf(filename, data)
    if suffix in IMAGE_SUFFIXES:
        return _ingest_image(filename, data)

    raise UnsupportedFile(
        f"{filename}: unsupported type '{suffix or 'none'}'. "
        "Supported: .drawio .xml .vsdx .pptx .pdf and common image formats."
    )


# ---------------------------------------------------------------------------
# draw.io / diagrams.net
# ---------------------------------------------------------------------------


def _inflate_drawio(payload: str) -> str | None:
    """draw.io stores diagrams deflate-compressed + base64 + URL-encoded."""
    import urllib.parse
    import zlib

    try:
        raw = base64.b64decode(payload)
        # raw deflate, no zlib header
        text = zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8")
        return urllib.parse.unquote(text)
    except Exception:
        return None


def _style_kind(style: str) -> str:
    """Pull the meaningful bit out of an mxGraph style string."""
    if not style:
        return ""
    for token in ("cylinder", "cloud", "actor", "component", "database", "note",
                  "ellipse", "rhombus", "hexagon", "process", "swimlane"):
        if token in style:
            return token
    if "shape=" in style:
        m = re.search(r"shape=([^;]+)", style)
        if m:
            return m.group(1).split(".")[-1]
    return ""


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html
    return html.unescape(text).strip()


def _ingest_drawio(filename: str, data: bytes) -> SourceDoc:
    root = ET.fromstring(data.decode("utf-8", errors="replace"))
    doc = SourceDoc(filename=filename, kind="structured")

    diagrams = root.findall(".//diagram") or [root]
    for i, dia in enumerate(diagrams):
        name = dia.get("name") or f"Page {i + 1}"

        graph_root = dia.find(".//mxGraphModel")
        if graph_root is None and (dia.text or "").strip():
            inflated = _inflate_drawio(dia.text.strip())
            if inflated:
                try:
                    graph_root = ET.fromstring(inflated).find(".//root") or ET.fromstring(inflated)
                except ET.ParseError:
                    graph_root = None
        if graph_root is None:
            continue

        page = Page(name=name)
        for cell in graph_root.iter("mxCell"):
            cid = cell.get("id") or ""
            style = cell.get("style") or ""
            label = _strip_html(cell.get("value") or "")

            if cell.get("edge") == "1":
                page.connectors.append(
                    Connector(
                        source=cell.get("source"),
                        target=cell.get("target"),
                        label=label,
                        directed="endArrow=none" not in style,
                    )
                )
            elif cell.get("vertex") == "1":
                geo = cell.find("mxGeometry")
                parent = cell.get("parent")
                page.shapes.append(
                    Shape(
                        id=cid,
                        text=label,
                        kind=_style_kind(style),
                        parent=parent if parent not in ("0", "1") else None,
                        x=float(geo.get("x", 0) or 0) if geo is not None else 0.0,
                        y=float(geo.get("y", 0) or 0) if geo is not None else 0.0,
                        w=float(geo.get("width", 0) or 0) if geo is not None else 0.0,
                        h=float(geo.get("height", 0) or 0) if geo is not None else 0.0,
                    )
                )

        if page.shapes or page.connectors:
            doc.pages.append(page)

    if not doc.pages:
        raise UnsupportedFile(f"{filename}: no diagram content found in XML.")
    return doc


# ---------------------------------------------------------------------------
# Visio .vsdx
# ---------------------------------------------------------------------------

_VSDX_NS = {"v": "http://schemas.microsoft.com/office/visio/2012/main"}


def _ingest_vsdx(filename: str, data: bytes) -> SourceDoc:
    doc = SourceDoc(filename=filename, kind="structured")

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        page_files = sorted(
            n for n in zf.namelist()
            if re.match(r"visio/pages/page\d+\.xml$", n)
        )
        names = _vsdx_page_names(zf)

        for idx, pf in enumerate(page_files):
            root = ET.fromstring(zf.read(pf))
            page = Page(name=names.get(idx, Path(pf).stem))

            for sh in root.findall(".//v:Shape", _VSDX_NS):
                sid = sh.get("ID") or ""
                texts = []
                for t in sh.findall(".//v:Text", _VSDX_NS):
                    texts.append("".join(t.itertext()))
                page.shapes.append(
                    Shape(
                        id=sid,
                        text=" ".join(texts).strip(),
                        kind=(sh.get("NameU") or "").rstrip("0123456789. "),
                    )
                )

            # Connects: FromSheet is the connector shape, ToSheet the endpoint.
            ends: dict[str, dict[str, str]] = {}
            for c in root.findall(".//v:Connect", _VSDX_NS):
                frm, to = c.get("FromSheet"), c.get("ToSheet")
                cell = c.get("FromCell") or ""
                if not frm or not to:
                    continue
                slot = "source" if "Begin" in cell else "target"
                ends.setdefault(frm, {})[slot] = to

            shape_text = {s.id: s.text for s in page.shapes}
            for conn_id, e in ends.items():
                page.connectors.append(
                    Connector(
                        source=e.get("source"),
                        target=e.get("target"),
                        label=shape_text.get(conn_id, ""),
                    )
                )
            # The connector shapes themselves aren't real nodes.
            connector_ids = set(ends)
            page.shapes = [s for s in page.shapes if s.id not in connector_ids]

            if page.shapes:
                doc.pages.append(page)

    if not doc.pages:
        raise UnsupportedFile(f"{filename}: no pages found in Visio file.")
    return doc


def _vsdx_page_names(zf: zipfile.ZipFile) -> dict[int, str]:
    try:
        root = ET.fromstring(zf.read("visio/pages/pages.xml"))
    except KeyError:
        return {}
    return {
        i: (p.get("Name") or p.get("NameU") or f"Page {i + 1}")
        for i, p in enumerate(root.findall(".//v:Page", _VSDX_NS))
    }


# ---------------------------------------------------------------------------
# PowerPoint .pptx
# ---------------------------------------------------------------------------


def _ingest_pptx(filename: str, data: bytes) -> SourceDoc:
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation(io.BytesIO(data))
    doc = SourceDoc(filename=filename, kind="structured")
    a_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

    for i, slide in enumerate(prs.slides):
        page = Page(name=f"Slide {i + 1}")

        for shape in slide.shapes:
            sid = str(shape.shape_id)
            text = shape.text_frame.text if shape.has_text_frame else ""
            kind = str(shape.shape_type).split(" ")[0].lower() if shape.shape_type else ""

            # Connectors carry <a:stCxn>/<a:endCxn> pointing at shape ids.
            st = shape._element.find(f".//{a_ns}stCxn")
            end = shape._element.find(f".//{a_ns}endCxn")
            if st is not None or end is not None:
                page.connectors.append(
                    Connector(
                        source=st.get("id") if st is not None else None,
                        target=end.get("id") if end is not None else None,
                        label=text.strip(),
                    )
                )
                continue

            if not text.strip():
                continue

            page.shapes.append(
                Shape(
                    id=sid,
                    text=text,
                    kind=kind,
                    x=Emu(shape.left).inches if shape.left is not None else 0.0,
                    y=Emu(shape.top).inches if shape.top is not None else 0.0,
                    w=Emu(shape.width).inches if shape.width is not None else 0.0,
                    h=Emu(shape.height).inches if shape.height is not None else 0.0,
                )
            )

        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            page.shapes.append(
                Shape(id=f"notes-{i}", text=slide.notes_slide.notes_text_frame.text,
                      kind="speaker-notes")
            )

        if page.shapes:
            doc.pages.append(page)

    if not doc.pages:
        raise UnsupportedFile(f"{filename}: no text content found in slides.")
    return doc


# ---------------------------------------------------------------------------
# PDF and images -> vision
# ---------------------------------------------------------------------------


def _ingest_pdf(filename: str, data: bytes) -> SourceDoc:
    import fitz  # PyMuPDF

    doc = SourceDoc(filename=filename, kind="visual")
    with fitz.open(stream=data, filetype="pdf") as pdf:
        if pdf.page_count > 20:
            doc.notes = f"first 20 of {pdf.page_count} pages"
        for page in pdf[:20]:
            pix = page.get_pixmap(dpi=PDF_RENDER_DPI)
            doc.images.append(_encode_png(pix.tobytes("png")))
    if not doc.images:
        raise UnsupportedFile(f"{filename}: PDF has no pages.")
    return doc


def _ingest_image(filename: str, data: bytes) -> SourceDoc:
    return SourceDoc(filename=filename, kind="visual", images=[_encode_png(data)])


def _encode_png(data: bytes) -> str:
    """Normalise to PNG and clamp the long edge to the model's ceiling."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    long_edge = max(img.size)
    if long_edge > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / long_edge
        img = img.resize((int(img.width * scale), int(img.height * scale)),
                         Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")
