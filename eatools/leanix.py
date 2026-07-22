"""Render extracted entities as LeanIX-shaped import CSVs.

LeanIX import templates vary per tenant (custom fields, renamed sections, the
exact relation column names your workspace uses). `SHEETS` below is the single
place to adjust for yours -- change a header string and the export follows.

Columns prefixed with `_` are review aids, not LeanIX fields. Drop them before
import, or leave them: LeanIX ignores columns it doesn't recognise.
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any, Callable

Row = dict[str, Any]
Getter = Callable[[Row], str]


def _f(key: str, default: str = "") -> Getter:
    """Plain field."""
    def get(row: Row) -> str:
        val = row.get(key, default)
        return "" if val is None or val == "unknown" else str(val)
    return get


def _list(key: str, sep: str = "; ") -> Getter:
    def get(row: Row) -> str:
        val = row.get(key) or []
        return sep.join(str(v) for v in val if v)
    return get


# .title() mangles acronyms and product-ish terms; spell these out.
ENUM_LABELS = {
    "saas": "SaaS",
    "api": "API",
    "etl": "ETL",
    "on_premise": "On Premise",
    "near_real_time": "Near Real-Time",
    "real_time": "Real-Time",
    "database_link": "Database Link",
}


def _enum(key: str) -> Getter:
    """Enum values come back snake_case; LeanIX shows them Title Case."""
    def get(row: Row) -> str:
        val = row.get(key)
        if not val or val == "unknown":
            return ""
        val = str(val)
        return ENUM_LABELS.get(val, val.replace("_", " ").title())
    return get


# Fact sheet type -> (source key in the extraction payload, column definitions)
SHEETS: dict[str, tuple[str, list[tuple[str, Getter]]]] = {
    "applications": ("applications", [
        ("type", lambda r: "Application"),
        ("name", _f("name")),
        ("alias", _f("alias")),
        ("description", _f("description")),
        ("businessCriticality", _enum("business_criticality")),
        ("lifecycle", _f("lifecycle")),
        ("hosting", _enum("hosting")),
        ("relApplicationToBusinessCapability", _list("capabilities")),
        ("relApplicationToDataObject", _list("data_objects")),
        ("relApplicationToITComponent", _list("it_components")),
        ("_evidence", _f("evidence")),
        ("_confidence", _f("confidence")),
    ]),
    "business_capabilities": ("capabilities", [
        ("type", lambda r: "BusinessCapability"),
        ("name", _f("name")),
        ("description", _f("description")),
        ("level", _f("level")),
        ("parent", _f("parent")),
        ("_evidence", _f("evidence")),
        ("_confidence", _f("confidence")),
    ]),
    "it_components": ("it_components", [
        ("type", lambda r: "ITComponent"),
        ("name", _f("name")),
        ("description", _f("description")),
        ("category", _enum("category")),
        ("_evidence", _f("evidence")),
        ("_confidence", _f("confidence")),
    ]),
    "data_objects": ("data_objects", [
        ("type", lambda r: "DataObject"),
        ("name", _f("name")),
        ("description", _f("description")),
        ("classification", _enum("classification")),
        ("_evidence", _f("evidence")),
        ("_confidence", _f("confidence")),
    ]),
    "interfaces": ("interfaces", [
        ("type", lambda r: "Interface"),
        ("name", _f("name")),
        ("description", _f("description")),
        ("relInterfaceToProviderApplication", _f("provider")),
        ("relInterfaceToConsumerApplication", _f("consumer")),
        ("relInterfaceToDataObject", _list("data_objects")),
        ("integrationType", _enum("integration_type")),
        ("frequency", _enum("frequency")),
        ("_evidence", _f("evidence")),
        ("_confidence", _f("confidence")),
    ]),
}

RELATIONS_COLUMNS = ["fromType", "fromName", "relation", "toType", "toName"]


def sheet_csv(name: str, payload: dict) -> str:
    source_key, columns = SHEETS[name]
    rows = payload.get(source_key) or []

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([header for header, _ in columns])
    for row in rows:
        writer.writerow([getter(row) for _, getter in columns])
    return buf.getvalue()


def relations_csv(payload: dict) -> str:
    """Flat edge list -- easier to eyeball and to load via the relations importer."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(RELATIONS_COLUMNS)

    for app in payload.get("applications") or []:
        for cap in app.get("capabilities") or []:
            writer.writerow(["Application", app.get("name", ""), "supports",
                             "BusinessCapability", cap])
        for obj in app.get("data_objects") or []:
            writer.writerow(["Application", app.get("name", ""), "uses",
                             "DataObject", obj])
        for comp in app.get("it_components") or []:
            writer.writerow(["Application", app.get("name", ""), "runs on",
                             "ITComponent", comp])

    for cap in payload.get("capabilities") or []:
        if cap.get("parent"):
            writer.writerow(["BusinessCapability", cap.get("name", ""), "child of",
                             "BusinessCapability", cap["parent"]])

    for iface in payload.get("interfaces") or []:
        name = iface.get("name", "")
        if iface.get("provider"):
            writer.writerow(["Interface", name, "provided by",
                             "Application", iface["provider"]])
        if iface.get("consumer"):
            writer.writerow(["Interface", name, "consumed by",
                             "Application", iface["consumer"]])
        for obj in iface.get("data_objects") or []:
            writer.writerow(["Interface", name, "carries", "DataObject", obj])

    return buf.getvalue()


def all_files(payload: dict) -> dict[str, str]:
    files = {f"{name}.csv": sheet_csv(name, payload) for name in SHEETS}
    files["relations.csv"] = relations_csv(payload)
    return files


def bundle_zip(payload: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in all_files(payload).items():
            # BOM so Excel opens UTF-8 cleanly on Windows.
            zf.writestr(filename, content.encode("utf-8-sig"))
        if payload.get("open_questions"):
            zf.writestr(
                "OPEN_QUESTIONS.txt",
                "\n".join(f"- {q}" for q in payload["open_questions"]).encode("utf-8"),
            )
    return buf.getvalue()


def counts(payload: dict) -> dict[str, int]:
    return {source_key: len(payload.get(source_key) or [])
            for source_key, _ in SHEETS.values()}
