"""Name matching robustness: spacing, case, punctuation, org suffixes and extra words.

Offline: no model calls (reconcile_fn stubbed or None), generated EDC template.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eatools.alfabet import read_edc  # noqa: E402
from eatools.merge import propose  # noqa: E402
from test_alfabet import TEMPLATE, _diagram_app, _payload, make_template  # noqa: E402


def _record(ref, name, platform=""):
    return ["", ref, name, "", "", "", "Active", "", "", platform, "Application", "1", "45000"]


def _alfabet(*names):
    return read_edc(TEMPLATE, make_template(rows=[_record(f"326-{i}-0", n) for i, n in enumerate(names)]))


def _pairs(sources, reconcile_fn=None, company="", notes=None):
    """{frozenset(name_a, name_b): proposal}"""
    result = propose(sources, reconcile_fn, company=company)
    if notes is not None:
        notes.extend(result["notes"])
    ents = {e["_id"]: e for s in sources for t in ("applications", "it_components") for e in s[t]}
    return {frozenset((ents[p["a"]]["name"], ents[p["b"]]["name"])): p for p in result["proposals"]}


def test_spacing_case_and_org_suffix_do_not_block_a_match():
    """The reported miss: 'SAP S/4 HANA' on a diagram vs 'SAP S/4HANA (NTTD EMEAL DACH)'."""
    sources = [_alfabet("SAP S/4HANA (NTTD EMEAL DACH)"), _payload(applications=[_diagram_app("SAP S/4 HANA")])]

    p = _pairs(sources).get(frozenset(("SAP S/4 HANA", "SAP S/4HANA (NTTD EMEAL DACH)")))

    assert p, "spacing differences must not hide the Alfabet record"
    assert p["method"] == "exact" and p["status"] == "accepted", p


def test_owner_suffix_is_not_part_of_the_name():
    """'(NTTD EMEAL DACH)' names the owning company; it must not create matches on its own."""
    sources = [
        _alfabet("Workflex (NTTD EMEAL DACH)", "Portal (NTT DATA)"),
        _payload(applications=[_diagram_app("NTTD EMEAL DACH"), _diagram_app("NTT DATA Hub")]),
    ]

    assert _pairs(sources) == {}, "owner words matched"


def test_a_diagram_naming_the_owner_gets_that_record():
    sources = [
        _alfabet("SAP S/4HANA (NTTD EMEAL DACH)", "SAP S/4HANA (NTT GN)"),
        _payload(applications=[_diagram_app("SAP S/4HANA (NTTD EMEAL DACH)")]),
    ]

    pairs = _pairs(sources)

    dach = pairs.get(frozenset(("SAP S/4HANA (NTTD EMEAL DACH)",)))  # same name both sides
    assert dach and dach["method"] == "exact" and dach["status"] == "accepted", pairs
    assert len(pairs) == 1, "the other owner's record is not offered"


def test_every_owner_variant_is_offered_for_partial_and_fuzzy_names():
    sources = [
        _alfabet("SAP S/4HANA (NTTD EMEAL DACH)", "SAP S/4HANA (NTT GN)"),
        _payload(applications=[_diagram_app("SAP S/4 HANA Finance"), _diagram_app("SAP S/4 HANAA")]),
    ]

    pairs = _pairs(sources)

    for diagram in ("SAP S/4 HANA Finance", "SAP S/4 HANAA"):
        for record in ("SAP S/4HANA (NTTD EMEAL DACH)", "SAP S/4HANA (NTT GN)"):
            assert frozenset((diagram, record)) in pairs, (diagram, record)


def test_names_differing_only_in_spacing_are_exact():
    sources = [
        _payload(applications=[_diagram_app("Data Hub")]),
        _payload(applications=[_diagram_app("datahub")]),
    ]

    p = _pairs(sources).get(frozenset(("Data Hub", "datahub")))

    assert p and p["method"] == "exact" and p["status"] == "accepted", p


def test_same_product_owned_by_several_orgs_stays_pending():
    sources = [
        _alfabet("SAP S/4HANA (NTTD EMEAL DACH)", "SAP S/4HANA (NTT GN)"),
        _payload(applications=[_diagram_app("SAP S/4 HANA")]),
    ]

    pairs = _pairs(sources)

    dach = pairs.get(frozenset(("SAP S/4 HANA", "SAP S/4HANA (NTTD EMEAL DACH)")))
    gn = pairs.get(frozenset(("SAP S/4 HANA", "SAP S/4HANA (NTT GN)")))
    assert dach and gn, "each candidate record is offered"
    assert dach["method"] == gn["method"] == "exact"
    assert dach["status"] == gn["status"] == "pending"


def test_extra_words_are_offered_as_pending_partial_matches():
    sources = [
        _alfabet("SAP S/4HANA Finance (NTT GN)", "Workday HCM (NTT GN)"),
        _payload(applications=[_diagram_app("SAP S/4 HANA"), _diagram_app("workday")]),
    ]

    pairs = _pairs(sources)

    for diagram, record in (("SAP S/4 HANA", "SAP S/4HANA Finance (NTT GN)"), ("workday", "Workday HCM (NTT GN)")):
        p = pairs.get(frozenset((diagram, record)))
        assert p, f"{diagram!r} should be offered against {record!r}"
        assert p["method"] == "partial" and p["status"] == "pending", p


def test_partial_matches_respect_word_boundaries_and_length():
    sources = [
        _alfabet("Metadata Manager (NTT GN)", "SAP Ariba (NTT GN)"),
        _payload(applications=[_diagram_app("Data"), _diagram_app("SAP")]),
    ]

    assert _pairs(sources) == {}, "'Data' is not a word of 'Metadata'; 'SAP' is too short to anchor on"


def test_partial_candidates_are_judged_by_claude():
    sources = [
        _alfabet("SAP S/4HANA Finance (NTT GN)"),
        _payload(applications=[_diagram_app("SAP S/4 HANA")]),
    ]
    seen = []

    def reconcile(type_key, candidates):
        seen.extend(c["name"] for c in candidates)
        return [["SAP S/4 HANA", "SAP S/4HANA Finance (NTT GN)"]] if type_key == "applications" else []

    p = _pairs(sources, reconcile).get(frozenset(("SAP S/4 HANA", "SAP S/4HANA Finance (NTT GN)")))

    assert "SAP S/4HANA Finance (NTT GN)" in seen, "partial candidates reach Claude"
    assert p["ai"] == "same" and p["status"] == "accepted", p


def test_claude_cannot_accept_a_partial_while_equal_names_are_ambiguous():
    """Seen on the DACH run: 8 'SAP S/4HANA (org)' records left pending, yet Claude's 'same'
    on 'SAP S/4HANA OneERP (...)' was auto-accepted -- a guess among nine candidates."""
    sources = [
        _alfabet("SAP S/4HANA (NTTD EMEAL DACH)", "SAP S/4HANA (NTT GN)", "SAP S/4HANA OneERP (NTTD EMEAL)"),
        _payload(applications=[_diagram_app("SAP S/4 HANA")]),
    ]

    def reconcile(type_key, candidates):
        return [[c["name"] for c in candidates]] if type_key == "applications" else []

    pairs = _pairs(sources, reconcile)

    one_erp = pairs.get(frozenset(("SAP S/4 HANA", "SAP S/4HANA OneERP (NTTD EMEAL)")))
    assert one_erp and one_erp["status"] == "pending", one_erp
    assert all(p["status"] == "pending" for p in pairs.values())


def test_alfabet_platform_matches_diagram_component_despite_spacing():
    alfabet = read_edc(TEMPLATE, make_template(rows=[_record("326-1-0", "Ledger (NTT GN)", "SAP S/4HANA (NTT GN)")]))
    diagram = _payload(it_components=[{
        "name": "SAP S/4 HANA", "description": "", "category": "unknown",
        "evidence": "box", "confidence": "high", "_source": "landscape.drawio",
    }])

    p = _pairs([alfabet, diagram]).get(frozenset(("SAP S/4 HANA", "SAP S/4HANA (NTT GN)")))

    assert p and p["status"] == "accepted", p


# ---------------------------------------------------------------------------
# Second round: the company narrows several equally good Alfabet candidates
# ---------------------------------------------------------------------------

OWNERS = ("SAP S/4HANA (NTTD EMEAL DACH)", "SAP S/4HANA (NTTD EMEAL)", "SAP S/4HANA (NTT GN)")


def _owners_case(source="landscape.drawio", name="SAP S/4 HANA"):
    return [_alfabet(*OWNERS), _payload(applications=[_diagram_app(name, _source=source)])]


def _statuses(pairs, diagram="SAP S/4 HANA"):
    return {record: pairs[frozenset((diagram, record))]["status"] for record in OWNERS}


def test_company_picks_one_of_several_equal_names():
    notes = []
    pairs = _pairs(_owners_case(), company="DACH", notes=notes)

    assert _statuses(pairs) == {
        "SAP S/4HANA (NTTD EMEAL DACH)": "accepted",
        "SAP S/4HANA (NTTD EMEAL)": "rejected",
        "SAP S/4HANA (NTT GN)": "rejected",
    }, _statuses(pairs)
    assert "DACH" in pairs[frozenset(("SAP S/4 HANA", OWNERS[0]))]["reason"]
    assert not any("pick the right one" in n for n in notes), "resolved -- no 'pick one' note"


def test_company_is_inferred_from_the_diagram_file_name():
    pairs = _pairs(_owners_case(source="DACH Architecture-V7.jpg"))

    assert _statuses(pairs)["SAP S/4HANA (NTTD EMEAL DACH)"] == "accepted", _statuses(pairs)
    assert "file name" in pairs[frozenset(("SAP S/4 HANA", OWNERS[0]))]["reason"]


def test_explicit_company_wins_over_the_file_name():
    pairs = _pairs(_owners_case(source="DACH Architecture-V7.jpg"), company="NTT GN")

    assert _statuses(pairs)["SAP S/4HANA (NTT GN)"] == "accepted", _statuses(pairs)


def test_company_shared_by_several_owners_narrows_without_picking():
    pairs = _pairs(_owners_case(), company="EMEAL")

    assert _statuses(pairs) == {
        "SAP S/4HANA (NTTD EMEAL DACH)": "pending",
        "SAP S/4HANA (NTTD EMEAL)": "pending",
        "SAP S/4HANA (NTT GN)": "rejected",
    }, _statuses(pairs)


def test_company_matches_whole_words_of_the_owner():
    """'EMEA' is not 'EMEAL'; nothing qualifies, so nothing changes."""
    notes = []
    pairs = _pairs(_owners_case(), company="EMEA", notes=notes)

    assert set(_statuses(pairs).values()) == {"pending"}
    assert any("EMEA" in n for n in notes), notes


def test_file_name_words_shared_by_every_candidate_do_not_decide():
    """'NTTD EMEAL landscape' fits two owners equally -- narrowing, not a pick."""
    pairs = _pairs(_owners_case(source="NTTD EMEAL landscape.pptx"))

    assert _statuses(pairs)["SAP S/4HANA (NTT GN)"] == "rejected"
    assert _statuses(pairs)["SAP S/4HANA (NTTD EMEAL DACH)"] == "pending"


def test_company_resolves_claude_matches_to_several_records():
    sources = [
        _alfabet("Workday HCM (NTTD EMEAL DACH)", "Workday HCM (NTT GN)"),
        _payload(applications=[_diagram_app("Workday")]),
    ]

    def reconcile(type_key, candidates):
        return [[c["name"] for c in candidates]] if type_key == "applications" else []

    pairs = _pairs(sources, reconcile, company="DACH")

    assert pairs[frozenset(("Workday", "Workday HCM (NTTD EMEAL DACH)"))]["status"] == "accepted"
    assert pairs[frozenset(("Workday", "Workday HCM (NTT GN)"))]["status"] == "rejected"


def test_merge_uses_the_company_pick():
    from eatools.merge import propose_and_merge

    result = propose_and_merge(_owners_case(), company="DACH")

    apps = {a["name"]: a for a in result["payload"]["applications"]}
    assert "SAP S/4 HANA" not in apps
    assert apps["SAP S/4HANA (NTTD EMEAL DACH)"]["_provenance"] == "SAP S/4 HANA; SAP S/4HANA (NTTD EMEAL DACH)"


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
