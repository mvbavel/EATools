"""Turn a (reviewed) payload dict into LeanIX import CSVs, relations, and graph files.

The `SHEETS` table below is **the tenant-adaptation seam**. LeanIX import templates differ
per workspace, so all column definitions live here as (header, value-getter) pairs — one
`SHEETS` entry per fact-sheet type. To retarget a different workspace, edit column headers
and getters here and nowhere else.

Conventions (per spec):

* Columns prefixed ``_`` (``_evidence``, ``_confidence``, ``_source`` …) are review aids,
  not LeanIX fields.
* Enum values render Title Case; ``unknown`` / empty render blank.
* Relation (list) columns join names with ``; ``.
* CSVs are written UTF-8 **with a BOM** so Excel opens them cleanly.

Adding an entity type is three edits that must stay in agreement: a schema block in
``extract.py``, a `SHEETS` entry here, and a `SHEETS` entry in ``frontend/app.js``.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from xml.sax.saxutils import escape


def _enum(value: str) -> str:
    """Title-case an enum; blank for unknown/empty."""
    if not value or value == "unknown":
        return ""
    return str(value).replace("_", " ").title()


def _join(values) -> str:
    return "; ".join(v for v in (values or []) if v)


def _text(value) -> str:
    return "" if value is None else str(value)


# Each column is (header, getter). Getters receive one entity dict.
_REVIEW_COLUMNS = [
    ("_evidence", lambda e: _text(e.get("evidence"))),
    ("_confidence", lambda e: _enum(e.get("confidence"))),
    ("_source", lambda e: _text(e.get("_source"))),
    ("_merged_from", lambda e: _text(e.get("_provenance"))),
]

SHEETS = {
    "applications": {
        "filename": "applications.csv",
        "label": "Applications",
        "columns": [
            ("Name", lambda e: _text(e.get("name"))),
            ("Alias", lambda e: _text(e.get("alias"))),
            ("Description", lambda e: _text(e.get("description"))),
            ("Business Criticality", lambda e: _enum(e.get("business_criticality"))),
            ("Lifecycle", lambda e: _enum(e.get("lifecycle"))),
            ("Hosting", lambda e: _enum(e.get("hosting"))),
            ("Business Capabilities", lambda e: _join(e.get("capabilities"))),
            ("Data Objects", lambda e: _join(e.get("data_objects"))),
            ("IT Components", lambda e: _join(e.get("it_components"))),
            *_REVIEW_COLUMNS,
        ],
    },
    "capabilities": {
        "filename": "business_capabilities.csv",
        "label": "Business Capabilities",
        "columns": [
            ("Name", lambda e: _text(e.get("name"))),
            ("Description", lambda e: _text(e.get("description"))),
            ("Level", lambda e: _text(e.get("level"))),
            ("Parent", lambda e: _text(e.get("parent"))),
            *_REVIEW_COLUMNS,
        ],
    },
    "it_components": {
        "filename": "it_components.csv",
        "label": "IT Components",
        "columns": [
            ("Name", lambda e: _text(e.get("name"))),
            ("Description", lambda e: _text(e.get("description"))),
            ("Category", lambda e: _enum(e.get("category"))),
            *_REVIEW_COLUMNS,
        ],
    },
    "data_objects": {
        "filename": "data_objects.csv",
        "label": "Data Objects",
        "columns": [
            ("Name", lambda e: _text(e.get("name"))),
            ("Description", lambda e: _text(e.get("description"))),
            ("Classification", lambda e: _enum(e.get("classification"))),
            *_REVIEW_COLUMNS,
        ],
    },
    "interfaces": {
        "filename": "interfaces.csv",
        "label": "Interfaces",
        "columns": [
            ("Name", lambda e: _text(e.get("name"))),
            ("Description", lambda e: _text(e.get("description"))),
            ("Provider", lambda e: _text(e.get("provider"))),
            ("Consumer", lambda e: _text(e.get("consumer"))),
            ("Data Objects", lambda e: _join(e.get("data_objects"))),
            ("Integration Type", lambda e: _enum(e.get("integration_type"))),
            ("Frequency", lambda e: _enum(e.get("frequency"))),
            *_REVIEW_COLUMNS,
        ],
    },
}


def sheet_csv(sheet_key: str, payload: dict) -> bytes:
    """Render one fact-sheet type as UTF-8 (BOM) CSV bytes."""
    if sheet_key not in SHEETS:
        raise KeyError(sheet_key)
    spec = SHEETS[sheet_key]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([header for header, _ in spec["columns"]])
    for entity in payload.get(sheet_key, []):
        writer.writerow([getter(entity) for _, getter in spec["columns"]])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _relations_csv(graph: dict) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["From Type", "From Name", "Relation", "To Type", "To Name"])
    for edge in graph.get("edges", []):
        writer.writerow(
            [
                _enum(edge.get("source_type", "")),
                edge.get("source_name", ""),
                _enum(edge.get("relation", "")),
                _enum(edge.get("target_type", "")),
                edge.get("target_name", ""),
            ]
        )
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _open_questions_txt(payload: dict) -> bytes:
    questions = payload.get("open_questions", [])
    body = "Open questions for reviewer:\n\n" + (
        "\n".join(f"- {q}" for q in questions) if questions else "(none)"
    )
    return body.encode("utf-8")


def graph_json(graph: dict) -> bytes:
    return json.dumps(graph, indent=2, ensure_ascii=False).encode("utf-8")


def graph_graphml(graph: dict) -> bytes:
    """Emit a minimal GraphML document with node/edge attributes."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '  <key id="type" for="node" attr.name="type" attr.type="string"/>',
        '  <key id="name" for="node" attr.name="name" attr.type="string"/>',
        '  <key id="sources" for="node" attr.name="sources" attr.type="string"/>',
        '  <key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
        '  <graph edgedefault="directed">',
    ]
    for node in graph.get("nodes", []):
        lines.append(f'    <node id="{escape(node["id"], {chr(34): "&quot;"})}">')
        lines.append(f'      <data key="type">{escape(node.get("type", ""))}</data>')
        lines.append(f'      <data key="name">{escape(node.get("name", ""))}</data>')
        lines.append(f'      <data key="sources">{escape(node.get("sources", ""))}</data>')
        lines.append("    </node>")
    for i, edge in enumerate(graph.get("edges", [])):
        s = escape(edge["source"], {chr(34): "&quot;"})
        t = escape(edge["target"], {chr(34): "&quot;"})
        lines.append(f'    <edge id="e{i}" source="{s}" target="{t}">')
        lines.append(f'      <data key="relation">{escape(edge.get("relation", ""))}</data>')
        lines.append("    </edge>")
    lines.append("  </graph>")
    lines.append("</graphml>")
    return "\n".join(lines).encode("utf-8")


def build_zip(payload: dict, graph: dict) -> bytes:
    """Bundle all CSVs + relations + open questions + graph files into a zip."""
    graph = graph or {"nodes": [], "edges": []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sheet_key, spec in SHEETS.items():
            zf.writestr(spec["filename"], sheet_csv(sheet_key, payload))
        zf.writestr("relations.csv", _relations_csv(graph))
        zf.writestr("OPEN_QUESTIONS.txt", _open_questions_txt(payload))
        zf.writestr("graph.json", graph_json(graph))
        zf.writestr("graph.graphml", graph_graphml(graph))
    return buf.getvalue()
