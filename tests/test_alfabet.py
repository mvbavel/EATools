"""Alfabet EDC import/export against a generated miniature EDC workbook.

The fixture mirrors the structure of a real Alfabet "Application" EDC template: a visible
``Export`` data sheet with list validations, ``Help`` with the Mandatory flags, a hidden
``DCTDReference`` caption map, and a hidden reference-data sheet behind the picklists.
Real templates hold company data, so none is checked in. Runs offline (no model calls).
"""

import io
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eatools.alfabet import EdcWorkbook, NotEdcWorkbook, build_edc, payload_from_api, read_edc  # noqa: E402
from eatools.ingest import UnsupportedFile  # noqa: E402
from eatools.merge import merge  # noqa: E402

TEMPLATE = "Application_All_TEST.xlsx"

# (caption, Alfabet property name or None for EDC system columns, picklist name or None)
COLUMNS = [
    ("Operations", None, "Operations"),
    ("Reference", None, None),
    ("Name", "Name", None),
    ("Alias", "SAG_Alias", None),
    ("Description", "Description", None),
    ("Criticality", "Criticality", "Criticality"),
    ("Object State", "ObjectState", "ObjectState"),
    ("Hosting Type", "HostingType", "HostingType"),
    ("Primary Capability", "Domain", "Domain"),
    ("Platform", "ICTObject", "ICTObject"),
    ("Stereotype", "Stereotype", "Stereotype"),
    ("Version", "Version", None),
    ("Start Date", "StartDate", None),
]
PICKLISTS = {
    "Operations": ["Create", "Update", "Delete", "No Change"],
    "Criticality": ["Mission Critical", "Business Critical", "Business Operational"],
    "ObjectState": ["Active", "Plan", "Retired"],
    "HostingType": ["Internally Hosted", "Externally Hosted", "Cloud Hosted", "Hybrid"],
    "Domain": ["1 Sales", "2 Finance"],
    "ICTObject": ["Oracle DB", "SAP S/4HANA"],
    "Stereotype": ["Application"],
}
MANDATORY = {"Name", "Object State", "Version", "Start Date"}
START_DATE_SERIAL = "45000"
ROWS = [
    ["", "326-1-0", "Billing Hub (NTT GN)", "BH", "Bills customers", "Business Critical",
     "Active", "Cloud Hosted", "2 Finance", "Oracle DB", "Application", "3.1", START_DATE_SERIAL],
    ["", "326-2-0", "Payroll (NTT GN)", "", "Pays staff", "Business Operational",
     "Active", "Externally Hosted", "2 Finance", "", "Application", "SaaS", START_DATE_SERIAL],
    ["", "326-3-0", "Payroll (NTT S)", "", "Pays staff", "Business Operational",
     "Active", "Externally Hosted", "2 Finance", "", "Application", "SaaS", START_DATE_SERIAL],
]

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col(i):
    out = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        out = chr(65 + rem) + out
    return out


def _sheet(rows, strings, extra="", date_cols=()):
    """Rows of strings -> worksheet XML using the shared-string table."""
    out = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_col(c)}{r}"
            if value == "":
                cells.append(f'<c r="{ref}" s="2"/>')
            elif c in date_cols and r > 1:
                cells.append(f'<c r="{ref}" s="3"><v>{value}</v></c>')
            else:
                if value not in strings:
                    strings.append(value)
                cells.append(f'<c r="{ref}" s="{1 if r == 1 else 2}" t="s"><v>{strings.index(value)}</v></c>')
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS}" xmlns:r="{_R}"><dimension ref="A1"/>'
        f'<sheetData>{"".join(out)}</sheetData>{extra}</worksheet>'
    )


def make_template(rows=ROWS, with_dctd=True):
    strings: list[str] = []
    header = [c[0] for c in COLUMNS]
    validations = "".join(
        f'<dataValidation type="list" sqref="{_col(i)}2:{_col(i)}1048576"><formula1>{pl}</formula1></dataValidation>'
        for i, (_, _, pl) in enumerate(COLUMNS)
        if pl
    )
    export = _sheet([header] + rows, strings, f"<dataValidations>{validations}</dataValidations>",
                    date_cols=(len(COLUMNS) - 1,))
    help_rows = [["Property Name", "Description", "Mandatory"]] + [
        [cap, "", "True" if cap in MANDATORY else "False"] for cap, prop, _ in COLUMNS if prop
    ]
    dctd_rows = [["DCTRef", "DCTDName", "DCTDCaption"]] + [
        [f"757-{i}-0", prop, cap] for i, (cap, prop, _) in enumerate(COLUMNS) if prop
    ]
    names = list(PICKLISTS)
    depth = max(len(v) for v in PICKLISTS.values())
    ref_rows = [[PICKLISTS[n][i] if i < len(PICKLISTS[n]) else "" for n in names] for i in range(depth)]
    defined = "".join(
        f'<definedName name="{n}">ReferenceData1!${_col(i)}$1:${_col(i)}${len(PICKLISTS[n])}</definedName>'
        for i, n in enumerate(names)
    )
    sheets = [("Export", None), ("Help", None)]
    if with_dctd:
        sheets.append(("DCTDReference", "veryHidden"))
    sheets.append(("ReferenceData1", "veryHidden"))
    parts = {
        "Export": export,
        "Help": _sheet(help_rows, strings),
        "DCTDReference": _sheet(dctd_rows, strings),
        "ReferenceData1": _sheet(ref_rows, strings),
    }

    sheet_xml = "".join(
        f'<sheet name="{n}" sheetId="{i}"{f" state={chr(34)}{st}{chr(34)}" if st else ""} r:id="rId{i}"/>'
        for i, (n, st) in enumerate(sheets, 1)
    )
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="{_R}/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i, _ in enumerate(sheets, 1)
    )
    sst = "".join(f"<si><t>{s.replace('&', '&amp;').replace('<', '&lt;')}</t></si>" for s in strings)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("docProps/custom.xml", '<Properties><property name="ALFA_RUNTIME_DCT"/></Properties>')
        zf.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{_NS}" xmlns:r="{_R}"><sheets>{sheet_xml}</sheets>'
            f"<definedNames>{defined}</definedNames></workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>',
        )
        for i, (n, _) in enumerate(sheets, 1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", parts[n])
        zf.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{_NS}" count="{len(strings)}" uniqueCount="{len(strings)}">{sst}</sst>',
        )
    return buf.getvalue()


def _diagram_app(name, **kw):
    app = {
        "name": name, "alias": "", "description": "", "business_criticality": "unknown",
        "lifecycle": "unknown", "hosting": "unknown", "capabilities": [], "data_objects": [],
        "it_components": [], "evidence": f"box '{name}'", "confidence": "high", "_source": "landscape.drawio",
    }
    app.update(kw)
    return app


def _payload(**entities):
    base = {"diagram_summary": "", "applications": [], "capabilities": [], "it_components": [],
            "data_objects": [], "interfaces": [], "open_questions": []}
    base.update(entities)
    return base


def _export(payload, template=None):
    """Run build_edc; return (workbook rows as dicts by caption, report text, xlsx bytes)."""
    template = template or make_template()
    with zipfile.ZipFile(io.BytesIO(build_edc(TEMPLATE, template, payload))) as zf:
        xlsx = zf.read("Application_All_TEST_EATools.xlsx")
        report = zf.read("alfabet_report.csv").decode("utf-8-sig")
    wb = EdcWorkbook("out.xlsx", xlsx)
    rows = [{wb.header.get(c): r.get(c) for c in wb.header.cells} for r in wb.rows]
    return rows, report, xlsx


# ---------------------------------------------------------------------------


def test_read_edc_maps_rows_to_entities():
    payload = read_edc(TEMPLATE, make_template())
    apps = {a["name"]: a for a in payload["applications"]}

    assert set(apps) == {"Billing Hub (NTT GN)", "Payroll (NTT GN)", "Payroll (NTT S)"}
    billing = apps["Billing Hub (NTT GN)"]
    assert billing["_alfabet_ref"] == "326-1-0"
    assert billing["business_criticality"] == "business_critical"
    assert billing["lifecycle"] == "active"
    assert billing["hosting"] == "cloud"
    assert billing["capabilities"] == ["2 Finance"]
    assert billing["it_components"] == ["Oracle DB"]
    assert "Billing Hub" in billing["_match_names"], billing["_match_names"]
    # "Externally Hosted" has no one-to-one EATools equivalent: unknown, not a guess.
    assert apps["Payroll (NTT GN)"]["hosting"] == "unknown"
    assert [c["name"] for c in payload["capabilities"]] == ["2 Finance"]
    assert [c["name"] for c in payload["it_components"]] == ["Oracle DB"]


def test_non_edc_workbooks_are_rejected_as_unsupported():
    for data in (b"not a zip", make_template(with_dctd=False)):
        try:
            read_edc("plain.xlsx", data)
        except NotEdcWorkbook as exc:
            assert isinstance(exc, UnsupportedFile), "app.py relies on catching UnsupportedFile"
        else:
            raise AssertionError("expected NotEdcWorkbook")


def test_diagram_app_matches_alfabet_app_by_base_name():
    alfabet = read_edc(TEMPLATE, make_template())
    diagram = _payload(applications=[_diagram_app("Billing Hub", business_criticality="mission_critical")])

    result = merge([alfabet, diagram], reconcile_fn=None)
    apps = {a["name"]: a for a in result["payload"]["applications"]}

    assert len(apps) == 3, sorted(apps)
    billing = apps["Billing Hub (NTT GN)"]  # the Alfabet name is kept
    assert billing["_alfabet_ref"] == "326-1-0"
    # Equal confidence: the system of record wins, and the disagreement is surfaced.
    assert billing["business_criticality"] == "business_critical"
    assert "business_criticality" in billing["_conflicts"]
    assert billing["_source"] == f"{TEMPLATE}; landscape.drawio"


def test_ambiguous_base_name_is_not_matched():
    """'Payroll' is the base name of two Alfabet records -- picking one would be a guess."""
    alfabet = read_edc(TEMPLATE, make_template())
    diagram = _payload(applications=[_diagram_app("Payroll")])

    apps = merge([alfabet, diagram], reconcile_fn=None)["payload"]["applications"]

    payroll = [a for a in apps if a["name"] == "Payroll"]
    assert len(apps) == 4 and payroll and payroll[0]["_alfabet_ref"] == ""


def test_reconciler_cannot_merge_two_alfabet_records():
    alfabet = read_edc(TEMPLATE, make_template())
    # Similar enough to both Payroll records to be sent to the reconciler.
    diagram = _payload(applications=[_diagram_app("Payroll NTT")])
    seen = []

    def reconcile(type_key, candidates):
        seen.extend(c["name"] for c in candidates)
        return [["Payroll NTT", "Payroll (NTT GN)", "Payroll (NTT S)"]] if type_key == "applications" else []

    result = merge([alfabet, diagram], reconcile_fn=reconcile)
    apps = result["payload"]["applications"]

    assert "Payroll (NTT S)" in seen, f"reconciler was not consulted: {seen}"
    assert len(apps) == 4, [a["name"] for a in apps]
    assert any("separate Alfabet records" in q for q in result["payload"]["open_questions"])


def test_round_trip_without_edits_writes_no_rows():
    """Import then export unchanged must not touch Alfabet: no spurious Update rows."""
    template = make_template()
    merged = merge([read_edc(TEMPLATE, template)], reconcile_fn=None)["payload"]

    rows, report, _ = _export(merged, template)

    assert rows == [], rows
    assert "0 create, 0 update, 3 matched but unchanged" in report, report


def test_export_update_keeps_existing_values():
    merged = merge([read_edc(TEMPLATE, make_template())], reconcile_fn=None)["payload"]
    billing = next(a for a in merged["applications"] if a["_alfabet_ref"] == "326-1-0")
    billing["business_criticality"] = "mission_critical"  # reviewer's edit
    billing["hosting"] = "unknown"  # blank in the payload must not clear Alfabet's value

    rows, report, _ = _export(merged)

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["Operations"] == "Update" and row["Reference"] == "326-1-0"
    assert row["Criticality"] == "Mission Critical"
    assert row["Hosting Type"] == "Cloud Hosted"
    assert row["Version"] == "3.1" and row["Start Date"] == START_DATE_SERIAL
    assert "'Business Critical' -> 'Mission Critical'" in report


def test_export_create_validates_picklists_and_flags_mandatory():
    new_app = _diagram_app(
        "Fraud Engine",
        lifecycle="phaseIn",
        hosting="saas",
        capabilities=["Risk", "Finance"],  # "Finance" matches "2 Finance" by its ordinal-free name
        it_components=["Kafka"],
    )
    rows, report, _ = _export(_payload(applications=[new_app]))

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["Operations"] == "Create" and row["Reference"] == ""
    assert row["Name"] == "Fraud Engine" and row["Stereotype"] == "Application"
    assert row["Primary Capability"] == "2 Finance"
    assert row["Object State"] == "Plan"
    assert row["Hosting Type"] == "" and row["Platform"] == ""
    assert "Hosting 'saas' has no unambiguous Alfabet Hosting Type" in report
    assert "none of ['Kafka'] is in the template picklist" in report
    assert "not known from the sources: Version, Start Date" in report


def test_export_flags_possible_duplicate_on_create():
    rows, report, _ = _export(_payload(applications=[_diagram_app("Billing Hub (NTT G)")]))

    assert rows[0]["Operations"] == "Create"
    assert "possible duplicate of existing: Billing Hub (NTT GN)" in report


def test_export_skips_ambiguous_reference():
    app = _diagram_app("Payroll", _alfabet_ref="326-2-0; 326-3-0")

    rows, report, _ = _export(_payload(applications=[app]))

    assert rows == []
    assert "matches several Alfabet objects" in report


def test_export_preserves_template_parts():
    template = make_template()
    merged = merge([read_edc(TEMPLATE, template)], reconcile_fn=None)["payload"]
    merged["applications"].append(_diagram_app("Fraud Engine & <Co>"))

    _, _, xlsx = _export(merged, template)

    with zipfile.ZipFile(io.BytesIO(template)) as src, zipfile.ZipFile(io.BytesIO(xlsx)) as out:
        assert src.namelist() == out.namelist()
        changed = {n for n in src.namelist() if src.read(n) != out.read(n)}
        assert changed == {"xl/worksheets/sheet1.xml", "xl/sharedStrings.xml"}, changed
        for name in changed:
            ET.fromstring(out.read(name))  # still well-formed XML


# Shape observed from the Alfabet REST API (report Application-Mark); values invented.
API_OBJECT = {
    "ClassName": "Application",
    "RefStr": "326-1-0",
    "Values": {
        "id": "APP-1", "name": "Billing Hub (NTT GN)", "shortname": "BH", "alias": "Billing",
        "description": "Bills customers", "objectstate": "Active", "hostingtype": "Cloud Hosted",
        "primarycapability": "Finance", "platform": "Oracle DB", "version": "3.1",
        "organizationbusinessowner": "NTT GN", "tier": None,
    },
    "NestedObjects": {},
}


def test_payload_from_api_maps_like_edc():
    payload = payload_from_api([API_OBJECT, {"ClassName": "Domain", "RefStr": "x", "Values": {"name": "D"}}], "Alfabet API")

    assert len(payload["applications"]) == 1, "non-Application objects are ignored"
    app = payload["applications"][0]
    assert app["_alfabet_ref"] == "326-1-0" and app["name"] == "Billing Hub (NTT GN)"
    assert app["lifecycle"] == "active" and app["hosting"] == "cloud"
    assert app["business_criticality"] == "unknown", "the report has no criticality column"
    assert app["capabilities"] == ["Finance"] and app["it_components"] == ["Oracle DB"]
    assert set(app["_match_names"]) == {"BH", "Billing Hub"}


def test_api_import_exports_as_update_against_edc_template():
    """API RefStr and EDC Reference are the same identifier, so the two paths combine."""
    merged = merge([payload_from_api([API_OBJECT], "Alfabet API")], reconcile_fn=None)["payload"]
    merged["applications"][0]["lifecycle"] = "endOfLife"

    rows, _, _ = _export(merged)

    assert len(rows) == 1 and rows[0]["Operations"] == "Update" and rows[0]["Reference"] == "326-1-0"
    assert rows[0]["Object State"] == "Retired"
    assert rows[0]["Primary Capability"] == "2 Finance", "unchanged value kept from the template"


def test_analyse_endpoint_imports_alfabet_selection_without_anthropic_key():
    import os

    from fastapi.testclient import TestClient

    import eatools.alfabet_api as api
    from eatools.app import app

    calls = []

    def fake_fetch(company="", name="", version="", objectstate=""):
        calls.append((company, name, version, objectstate))
        return [API_OBJECT], api.selection_args(company, name, version, objectstate)

    saved_fetch, api.fetch_selection = api.fetch_selection, fake_fetch
    saved_env = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        res = TestClient(app).post(
            "/api/analyse",
            data={"alfabet_import": "true", "alfabet_company": "NTT GN", "alfabet_objectstate": "Active"},
        )
    finally:
        api.fetch_selection = saved_fetch
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v

    assert res.status_code == 200, res.text[:300]
    data = res.json()
    assert calls == [("NTT GN", "", "", "Active")]
    assert [a["_alfabet_ref"] for a in data["applications"]] == ["326-1-0"]
    assert data["_sources"][0]["kind"] == "alfabet" and "name=*NTT GN*" in data["_sources"][0]["name"]


def test_export_endpoint_accepts_payload_over_one_megabyte():
    """A full Alfabet import is MBs of JSON; Starlette caps plain form fields at 1 MB."""
    import json

    from fastapi.testclient import TestClient

    from eatools.app import app

    big = _payload(applications=[_diagram_app(f"App {i}", description="x" * 400) for i in range(3000)])
    body = json.dumps({"payload": big, "authorisation": {"by": "M. Tester"}}).encode()
    assert len(body) > 1024 * 1024

    res = TestClient(app).post(
        "/api/export/alfabet",
        files={"template": (TEMPLATE, make_template()), "body": ("payload.json", body, "application/json")},
    )

    assert res.status_code == 200, f"{res.status_code}: {res.text[:200]}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface the crash under test
                failures += 1
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print("all passed" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
