"""Cross-document merge behaviour, exercised without any model call.

`merge()` takes `reconcile_fn=None` to skip Claude reconciliation, so these run offline
and deterministically -- only the name/alias grouping path is under test.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eatools.merge import merge  # noqa: E402


def _capability(name, level, confidence="high", source="a.drawio", parent=""):
    return {
        "name": name,
        "description": "",
        "level": level,
        "parent": parent,
        "evidence": f"box '{name}'",
        "confidence": confidence,
        "_source": source,
    }


def _payload(**entities):
    base = {
        "diagram_summary": "",
        "applications": [],
        "capabilities": [],
        "it_components": [],
        "data_objects": [],
        "interfaces": [],
        "open_questions": [],
    }
    base.update(entities)
    return base


def test_capability_level_is_numeric():
    """`level` is an integer enum in the schema, so merging must not treat it as a string."""
    docs = [
        _payload(capabilities=[_capability("Billing", 2, source="a.drawio")]),
        _payload(capabilities=[_capability("Billing", 2, source="b.vsdx")]),
    ]

    result = merge(docs, reconcile_fn=None)

    caps = result["payload"]["capabilities"]
    assert len(caps) == 1, f"same capability in two docs should merge into one, got {len(caps)}"
    assert caps[0]["level"] == 2, f"level should survive as 2, got {caps[0]['level']!r}"


def test_level_conflict_resolves_to_highest_confidence():
    docs = [
        _payload(capabilities=[_capability("Billing", 3, confidence="low", source="a.drawio")]),
        _payload(capabilities=[_capability("Billing", 1, confidence="high", source="b.vsdx")]),
    ]

    caps = merge(docs, reconcile_fn=None)["payload"]["capabilities"]

    assert len(caps) == 1
    assert caps[0]["level"] == 1, f"high-confidence level should win, got {caps[0]['level']!r}"


def test_corroboration_bumps_confidence():
    """Same entity seen in two documents is more trustworthy than in one."""
    docs = [
        _payload(capabilities=[_capability("Billing", 2, confidence="medium", source="a.drawio")]),
        _payload(capabilities=[_capability("Billing", 2, confidence="medium", source="b.vsdx")]),
    ]

    caps = merge(docs, reconcile_fn=None)["payload"]["capabilities"]

    assert caps[0]["confidence"] == "high", f"expected bump to high, got {caps[0]['confidence']}"
    assert caps[0]["_source"] == "a.drawio; b.vsdx"


def test_empty_parent_stays_blank():
    """A level-1 capability has no parent; "unknown" would import as a phantom parent."""
    docs = [_payload(capabilities=[_capability("Billing", 1, parent="")])]

    caps = merge(docs, reconcile_fn=None)["payload"]["capabilities"]

    assert caps[0]["parent"] == "", f"expected blank parent, got {caps[0]['parent']!r}"


def test_blank_parent_draws_no_graph_edge():
    docs = [_payload(capabilities=[_capability("Billing", 1, parent="")])]

    graph = merge(docs, reconcile_fn=None)["graph"]

    child_of = [e for e in graph["edges"] if e.get("relation") == "child_of"]
    assert not child_of, f"parentless capability should have no child_of edge, got {child_of}"
    names = {n.get("name") for n in graph["nodes"]}
    assert "unknown" not in names, "'unknown' leaked into the graph as a node"


def test_unlabelled_interface_endpoints_stay_blank():
    """provider/consumer are application-name references, not enums."""
    interface = {
        "name": "Billing to CRM feed",
        "description": "",
        "provider": "",
        "consumer": "",
        "integration_type": "unknown",
        "frequency": "unknown",
        "data_objects": [],
        "evidence": "arrow",
        "confidence": "low",
        "_source": "a.drawio",
    }

    ifaces = merge([_payload(interfaces=[interface])], reconcile_fn=None)["payload"]["interfaces"]

    assert ifaces[0]["provider"] == "", f"got {ifaces[0]['provider']!r}"
    assert ifaces[0]["consumer"] == "", f"got {ifaces[0]['consumer']!r}"
    # True enums keep the "unknown" sentinel -- only name references go blank.
    assert ifaces[0]["integration_type"] == "unknown"


def test_conflicting_parents_reported():
    docs = [
        _payload(capabilities=[_capability("Billing", 2, parent="Finance", source="a.drawio")]),
        _payload(capabilities=[_capability("Billing", 2, parent="Operations", source="b.vsdx")]),
    ]

    result = merge(docs, reconcile_fn=None)
    caps = result["payload"]["capabilities"]

    assert "parent" in caps[0]["_conflicts"], f"expected a parent conflict, got {caps[0]['_conflicts']!r}"
    assert any("Billing" in q for q in result["payload"]["open_questions"]), \
        "a conflict should surface as an open question"


def test_single_document_level_untouched():
    docs = [_payload(capabilities=[_capability("Billing", 3)])]

    caps = merge(docs, reconcile_fn=None)["payload"]["capabilities"]

    assert caps[0]["level"] == 3


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
