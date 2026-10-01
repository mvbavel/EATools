"""Match proposals, reviewer decisions, write-back preview and authorised export.

Offline: no model calls (reconcile_fn stubbed or None), generated EDC template.
"""

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eatools.alfabet import build_edc, plan_edc, read_edc  # noqa: E402
from eatools.merge import merge, propose  # noqa: E402
from test_alfabet import TEMPLATE, _diagram_app, _payload, make_template  # noqa: E402


def _by_name(payload, type_key="applications"):
    return {e["name"]: e for e in payload[type_key]}


def _sources():
    """Alfabet import + one diagram, the shape the Matches tab works on."""
    alfabet = read_edc(TEMPLATE, make_template())
    diagram = _payload(applications=[
        _diagram_app("Billing Hub"),          # org-less name of an Alfabet record -> alias
        _diagram_app("Payroll"),              # org-less name of TWO records -> ambiguous
        _diagram_app("Fraud Engine"),         # new
        _diagram_app("Fraud Engin", confidence="low"),  # typo -> fuzzy
    ])
    return [alfabet, diagram]


def _find(proposals, name_a, name_b, entities):
    for p in proposals:
        names = {entities[p["a"]]["name"], entities[p["b"]]["name"]}
        if names == {name_a, name_b}:
            return p
    return None


def _entities(sources):
    return {e["_id"]: e for s in sources for t in ("applications", "capabilities", "it_components") for e in s[t]}


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


def test_proposals_cover_alias_ambiguous_and_fuzzy():
    sources = _sources()
    result = propose(sources)
    props, ents = result["proposals"], _entities(sources)

    alias = _find(props, "Billing Hub", "Billing Hub (NTT GN)", ents)
    assert alias and alias["method"] == "alias" and alias["status"] == "accepted", alias

    gn = _find(props, "Payroll", "Payroll (NTT GN)", ents)
    s = _find(props, "Payroll", "Payroll (NTT S)", ents)
    assert gn and s, "an ambiguous org-less name is offered against each record"
    assert gn["status"] == s["status"] == "pending"

    fuzzy = _find(props, "Fraud Engine", "Fraud Engin", ents)
    assert fuzzy and fuzzy["method"] == "fuzzy" and fuzzy["status"] == "pending"
    assert 0.82 <= fuzzy["score"] < 1

    assert _find(props, "Payroll (NTT GN)", "Payroll (NTT S)", ents) is None, \
        "two Alfabet records are never proposed as one"
    assert len({p["id"] for p in props}) == len(props)


def test_ai_confirmed_fuzzy_match_is_preaccepted():
    sources = _sources()

    def reconcile(type_key, candidates):
        return [["Fraud Engine", "Fraud Engin"]] if type_key == "applications" else []

    props, ents = propose(sources, reconcile)["proposals"], _entities(sources)
    fuzzy = _find(props, "Fraud Engine", "Fraud Engin", ents)
    assert fuzzy["ai"] == "same" and fuzzy["status"] == "accepted"


def test_duplicate_alfabet_names_never_auto_matched():
    rows = [
        ["", "326-8-0", "Percipio (NTTD)", "", "", "", "Active", "", "", "", "Application", "1", "45000"],
        ["", "326-9-0", "Percipio (NTTD)", "", "", "", "Active", "", "", "", "Application", "1", "45000"],
    ]
    alfabet = read_edc(TEMPLATE, make_template(rows=rows))
    diagram = _payload(applications=[_diagram_app("Percipio (NTTD)")])

    result = propose([alfabet, diagram])

    exact = [p for p in result["proposals"] if p["method"] == "exact"]
    assert len(exact) == 2 and all(p["status"] == "pending" for p in exact), exact
    assert result["notes"], "the reviewer is told why nothing was decided"


def test_alfabet_records_are_never_matched_with_each_other():
    """Alfabet platforms/capabilities carry no Reference but are still distinct records:
    an Alfabet-only import must propose nothing and never call Claude."""
    rows = [
        ["", f"326-{i}-0", f"App {i} (NTT)", "", "", "", "Active", "", "2 Finance", platform,
         "Application", "1", "45000"]
        for i, platform in enumerate(["SAP S/4HANA (NTT GN)", "SAP S/4HANA (NTT S)"])
    ]
    alfabet = read_edc(TEMPLATE, make_template(rows=rows))
    calls = []

    result = propose([alfabet], lambda t, c: calls.append(t) or [])

    assert result["proposals"] == [], result["proposals"]
    assert calls == [], "no Claude call for an Alfabet-only import"


# ---------------------------------------------------------------------------
# Decisions -> merge
# ---------------------------------------------------------------------------


def test_merge_uses_only_accepted_pairs():
    sources = _sources()
    props, ents = propose(sources)["proposals"], _entities(sources)
    fuzzy = _find(props, "Fraud Engine", "Fraud Engin", ents)
    alias = _find(props, "Billing Hub", "Billing Hub (NTT GN)", ents)

    # Reviewer: accept the typo match, reject the alias match.
    accepted = [(p["a"], p["b"]) for p in props if p["status"] == "accepted" and p is not alias]
    accepted.append((fuzzy["a"], fuzzy["b"]))
    apps = _by_name(merge(sources, accepted=accepted)["payload"])

    assert "Fraud Engin" not in apps and apps["Fraud Engine"]["_provenance"] == "Fraud Engin; Fraud Engine"
    assert "Billing Hub" in apps and apps["Billing Hub"]["_alfabet_ref"] == "", "rejected match stays separate"
    assert apps["Billing Hub (NTT GN)"]["_origin"] == "alfabet"


def test_accepting_two_records_for_one_entity_is_blocked():
    sources = _sources()
    props, ents = propose(sources)["proposals"], _entities(sources)
    gn = _find(props, "Payroll", "Payroll (NTT GN)", ents)
    s = _find(props, "Payroll", "Payroll (NTT S)", ents)

    result = merge(sources, accepted=[(gn["a"], gn["b"]), (s["a"], s["b"])])

    assert result["blocked"] == [[s["a"], s["b"]]], result["blocked"]
    payroll = [a for a in result["payload"]["applications"] if a["name"].startswith("Payroll")]
    assert sorted(a["_alfabet_ref"] for a in payroll) == ["326-2-0", "326-3-0"]


def test_rejected_exact_match_keeps_graph_ids_unique():
    a = _payload(capabilities=[{"name": "Billing", "description": "", "level": 1, "parent": "",
                                "evidence": "", "confidence": "high", "_source": "a.drawio"}])
    b = json.loads(json.dumps(a))
    b["capabilities"][0]["_source"] = "b.drawio"

    graph = merge([a, b], accepted=[])["graph"]

    ids = [n["id"] for n in graph["nodes"]]
    assert len(ids) == 2 and len(set(ids)) == 2, ids


def test_merge_endpoint_round_trip():
    from fastapi.testclient import TestClient

    from eatools.app import app

    sources = _sources()
    props, ents = propose(sources)["proposals"], _entities(sources)
    fuzzy = _find(props, "Fraud Engine", "Fraud Engin", ents)

    res = TestClient(app).post("/api/merge", json={"sources": sources, "accepted": [[fuzzy["a"], fuzzy["b"]]]})

    assert res.status_code == 200, res.text[:300]
    names = {a["name"] for a in res.json()["applications"]}
    assert "Fraud Engin" not in names and "Billing Hub" in names
    assert TestClient(app).post("/api/merge", json={"sources": "x"}).status_code == 400


# ---------------------------------------------------------------------------
# Write-back preview and authorised export
# ---------------------------------------------------------------------------


def _merged_for_export():
    template = make_template()
    merged = merge([read_edc(TEMPLATE, template)], reconcile_fn=None)["payload"]
    billing = next(a for a in merged["applications"] if a["_alfabet_ref"] == "326-1-0")
    billing["business_criticality"] = "mission_critical"
    merged["applications"].append(_diagram_app("Fraud Engine", _id="new-1"))
    return template, merged, billing


def test_plan_lists_changes_without_writing():
    template, merged, billing = _merged_for_export()

    plan = plan_edc(TEMPLATE, template, merged)

    ops = {r["name"]: r for r in plan["rows"]}
    assert ops["Billing Hub (NTT GN)"]["operation"] == "Update"
    assert ops["Billing Hub (NTT GN)"]["id"] == billing["_id"]
    assert any("Mission Critical" in n for n in ops["Billing Hub (NTT GN)"]["notes"])
    assert ops["Fraud Engine"]["operation"] == "Create" and ops["Fraud Engine"]["id"] == "new-1"
    assert plan["counts"]["Unchanged"] == 2


def test_only_authorised_rows_are_written():
    template, merged, billing = _merged_for_export()

    out = build_edc(TEMPLATE, template, merged, authorised_ids=["new-1"],
                    authorisation={"by": "M. Tester", "at": "2026-10-01 12:00 UTC"})

    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        report = zf.read("alfabet_report.csv").decode("utf-8-sig")
        xlsx = zf.read("Application_All_TEST_EATools.xlsx")
    from eatools.alfabet import EdcWorkbook

    wb = EdcWorkbook("o.xlsx", xlsx)
    assert [r.get(wb.col_of["Name"]) for r in wb.rows] == ["Fraud Engine"]
    assert "Authorised by: M. Tester" in report
    assert "Billing Hub (NTT GN),Not authorised" in report
    assert "1 not authorised" in report


def test_export_endpoints_require_authorisation():
    from fastapi.testclient import TestClient

    from eatools.app import app

    client = TestClient(app)
    template, merged, _ = _merged_for_export()

    assert client.post("/api/export", json={"payload": merged}).status_code == 400
    res = client.post("/api/export", json={"payload": merged, "authorisation": {"by": "M. Tester"}})
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert "Authorised by: M. Tester" in zf.read("AUTHORISATION.txt").decode()

    def edc(body):
        return client.post("/api/export/alfabet", files={
            "template": (TEMPLATE, template),
            "body": ("payload.json", json.dumps(body).encode(), "application/json"),
        })

    assert edc({"payload": merged}).status_code == 400
    assert edc({"payload": merged, "authorisation": {"by": "M. Tester"}, "authorised_ids": ["new-1"]}).status_code == 200

    plan = client.post("/api/alfabet/plan", files={
        "template": (TEMPLATE, template),
        "body": ("payload.json", json.dumps({"payload": merged}).encode(), "application/json"),
    })
    assert plan.status_code == 200 and plan.json()["counts"]["Create"] == 1


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
