"""Consolidate per-document extractions into one picture via an in-memory graph.

`merge(per_doc_payloads, client)` takes the payloads that `extract.py` produced (one per
uploaded file, each entity tagged with `_source`) and returns::

    {"payload": <consolidated payload dict>, "graph": <nodes+edges>, "merge_report": [...]}

Resolution is **hybrid**:

1. *Deterministic grouping* — entities whose names (or, for applications, aliases)
   normalise to the same canonical key are merged immediately.
2. *Ambiguous-cluster detection* — remaining names that are similar (stdlib
   ``difflib``) but not identical become candidates.
3. *Claude reconciliation* — one schema-constrained call per type decides which
   candidates are genuinely the same entity (e.g. "SAP" vs "SAP ERP" vs "SAP S/4HANA").

Everything is in-memory and per-request: the graph is built, returned to the browser,
and discarded. Nothing is written to disk — the server stays stateless.

The reconciliation step is injected as `reconcile_fn` so it can be stubbed in tests;
`app.py` passes a real `client` and the default LLM reconciler is used.
"""

from __future__ import annotations

import difflib
import re

import anthropic

from .schema import ENTITY_KEYS

SIMILARITY_THRESHOLD = 0.82
_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}
_RANK_CONF = {3: "high", 2: "medium", 1: "low"}

# Identity of an entity imported from a system of record (see alfabet.py). Two entities
# with different references are two real objects: they are never merged, their name wins
# a merged group, and their values win confidence ties.
_EXTERNAL_REF = "_alfabet_ref"

# Which reference fields on each entity type point at which other type. Used to rewrite
# cross-references to canonical names and to build graph edges.
_REFERENCES = {
    "applications": [
        ("capabilities", "capabilities", "realizes"),
        ("data_objects", "data_objects", "uses_data"),
        ("it_components", "it_components", "runs_on"),
    ],
}


def _canon(name: str) -> str:
    """Normalise a name for matching: casefold, strip, collapse non-alphanumerics."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


# ---------------------------------------------------------------------------
# LLM reconciliation
# ---------------------------------------------------------------------------

_RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "description": "Groups of names that refer to the SAME real-world entity. Omit singletons.",
            "items": {
                "type": "object",
                "properties": {
                    "members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Two or more of the candidate names that are the same entity.",
                    },
                    "canonical": {"type": "string", "description": "The best single name for the group."},
                },
                "required": ["members", "canonical"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}

_RECONCILE_SYSTEM = """\
You are an enterprise architect deduplicating entities extracted from several architecture \
diagrams of the same landscape. You are given candidate names of one entity type that look \
similar. Decide which of them refer to the SAME real-world entity and should be merged.

Be conservative: only group names you are confident denote the same thing (e.g. "SAP", \
"SAP ERP", "SAP S/4HANA" are the same system; "Billing Service" and "Billing Database" are \
NOT). Return only groups of two or more; do not list singletons."""


def _llm_reconcile(client: anthropic.Anthropic):
    """Return a reconcile_fn bound to a live client."""

    def reconcile(type_key: str, candidates: list[dict]) -> list[list[str]]:
        import json

        lines = [
            f"- {c['name']}"
            + (f" — {c['description']}" if c.get("description") else "")
            + (f" (seen in: {c['sources']})" if c.get("sources") else "")
            for c in candidates
        ]
        prompt = (
            f"Entity type: {type_key}\nCandidate names:\n" + "\n".join(lines)
        )
        try:
            with client.messages.stream(
                model="claude-opus-4-8",
                max_tokens=4000,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": _RECONCILE_SCHEMA},
                },
                system=[{"type": "text", "text": _RECONCILE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
            text = next((b.text for b in message.content if b.type == "text"), "{}")
            groups = json.loads(text).get("groups", [])
        except Exception:
            # Reconciliation is best-effort; a failure just leaves candidates unmerged.
            return []
        return [g["members"] for g in groups if len(g.get("members", [])) >= 2]

    return reconcile


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _alt_names(ent: dict) -> set[str]:
    """Canonical alias + match names (e.g. an Alfabet name without its org suffix)."""
    names = [ent.get("alias", "")] + list(ent.get("_match_names") or [])
    return {_canon(n) for n in names if _canon(n)}


def _group_type(
    entities: list[dict], type_key: str, reconcile_fn
) -> tuple[_UnionFind, dict[str, dict], list[str]]:
    """Union-find over canonical keys for one entity type.

    Returns the union-find, a representative entity per key, and open-question notes for
    matches that were too ambiguous to make.
    """
    uf = _UnionFind()
    by_key: dict[str, dict] = {}  # canonical key -> a representative entity (highest confidence)
    refs: dict[str, set[str]] = {}  # union-find root -> external references in that group
    notes: list[str] = []

    for ent in entities:
        key = _canon(ent.get("name", ""))
        if not key:
            continue
        uf.add(key)
        if ent.get(_EXTERNAL_REF):
            refs.setdefault(key, set()).add(ent[_EXTERNAL_REF])
        cur = by_key.get(key)
        if cur is None or _CONF_RANK.get(ent.get("confidence", ""), 0) > _CONF_RANK.get(cur.get("confidence", ""), 0):
            by_key[key] = ent

    def safe_union(a: str, b: str) -> bool:
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            return True
        sa, sb = refs.get(ra, set()), refs.get(rb, set())
        if sa and sb and sa != sb:
            return False
        uf.union(ra, rb)
        refs.pop(ra, None)
        if sa | sb:
            refs[uf.find(rb)] = sa | sb
        return True

    # Deterministic: link an entity to another whose *name* equals one of its aliases or
    # match names. A name claimed by two different system-of-record objects (e.g. the
    # same product run by two orgs) is ambiguous and left to similarity matching.
    if type_key == "applications":
        claimants: dict[str, set[str]] = {}
        for ent in entities:
            if ent.get(_EXTERNAL_REF):
                for alt in _alt_names(ent):
                    claimants.setdefault(alt, set()).add(ent[_EXTERNAL_REF])
        for ent in entities:
            name = _canon(ent.get("name", ""))
            for alt in _alt_names(ent):
                if name and alt in by_key and alt != name and len(claimants.get(alt, ())) <= 1:
                    safe_union(name, alt)

    # Ambiguous clusters: similar-but-not-identical keys → candidates for the LLM. Two
    # system-of-record entities can never merge, so those pairs are skipped -- with a
    # 2,500-row Alfabet import that is millions of comparisons avoided.
    keys = sorted(by_key)
    anchored = {k for k in keys if refs.get(uf.find(k))}
    involved: set[str] = set()
    matcher = difflib.SequenceMatcher(None)
    for a in keys:
        if a in anchored:
            continue
        matcher.set_seq2(a)
        for b in keys:
            if b == a or (b not in anchored and b < a) or uf.find(a) == uf.find(b):
                continue
            matcher.set_seq1(b)
            if (
                matcher.real_quick_ratio() >= SIMILARITY_THRESHOLD
                and matcher.quick_ratio() >= SIMILARITY_THRESHOLD
                and matcher.ratio() >= SIMILARITY_THRESHOLD
            ):
                involved.add(a)
                involved.add(b)

    if involved and reconcile_fn is not None:
        candidates = [
            {
                "name": by_key[k].get("name", ""),
                "description": by_key[k].get("description", ""),
                "sources": by_key[k].get("_source", ""),
            }
            for k in sorted(involved)
        ]
        for member_names in reconcile_fn(type_key, candidates):
            member_keys = [_canon(m) for m in member_names if _canon(m) in by_key]
            distinct = {frozenset(refs[uf.find(k)]) for k in member_keys if refs.get(uf.find(k))}
            if len(distinct) > 1:
                notes.append(
                    f"Possible match among {type_key}: {', '.join(member_names)} -- several are "
                    "separate Alfabet records, so nothing was merged; set _alfabet_ref by hand if one is right."
                )
                continue
            for other in member_keys[1:]:
                safe_union(member_keys[0], other)

    return uf, by_key, notes


def _pick_canonical_name(members: list[dict]) -> str:
    """Best display name: the system-of-record name, else highest confidence, then longest."""
    return max(
        members,
        key=lambda e: (
            bool(e.get(_EXTERNAL_REF)),
            _CONF_RANK.get(e.get("confidence", ""), 0),
            len(e.get("name", "")),
        ),
    ).get("name", "")


def _merge_field(members: list[dict], field: str) -> tuple[str, list[str]]:
    """Pick the highest-confidence non-empty/unknown value; report conflicts."""
    best_val, best_rank = "", -1.0
    seen: set[str] = set()
    for ent in members:
        # Not every schema field is a string -- `capabilities.level` is an int enum.
        raw = ent.get(field)
        val = "" if raw is None else str(raw).strip()
        if not val or val == "unknown":
            continue
        seen.add(val)
        # The system of record wins a tie: the diagram must say so with more confidence.
        rank = _CONF_RANK.get(ent.get("confidence", ""), 0) + (0.5 if ent.get(_EXTERNAL_REF) else 0)
        if rank > best_rank:
            best_val, best_rank = val, rank
    conflicts = sorted(seen) if len(seen) > 1 else []
    return best_val, conflicts


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

_LIST_FIELDS = {
    "applications": ["capabilities", "data_objects", "it_components"],
    "interfaces": ["data_objects"],
}
_SCALAR_ENUMS = {
    "applications": ["business_criticality", "lifecycle", "hosting"],
    "capabilities": ["level"],
    "it_components": ["category"],
    "data_objects": ["classification"],
    "interfaces": ["integration_type", "frequency"],
}
# Scalar fields holding another entity's *name*. Merged as text: a missing value stays
# empty, never "unknown" -- that string would import as a phantom parent/endpoint and
# would have `_build_graph` draw an edge to a node that does not exist.
_SCALAR_REFS = {
    "capabilities": ["parent"],
    "interfaces": ["provider", "consumer"],
}


def merge(per_doc_payloads: list[dict], client: anthropic.Anthropic | None = None, reconcile_fn=None):
    """Consolidate per-document payloads. Returns {payload, graph, merge_report}."""
    if reconcile_fn is None and client is not None:
        reconcile_fn = _llm_reconcile(client)

    merged_payload: dict = {"diagram_summary": "", "open_questions": []}
    merge_report: list[dict] = []
    name_maps: dict[str, dict[str, str]] = {}  # type_key -> canon(name) -> canonical display name

    # Diagram summaries: keep each, prefixed by source.
    summaries = []
    for p in per_doc_payloads:
        s = (p.get("diagram_summary") or "").strip()
        if s:
            src = next(
                (e.get("_source") for k in ENTITY_KEYS for e in p.get(k, []) if e.get("_source")),
                "",
            )
            summaries.append(f"[{src}] {s}" if src else s)
    merged_payload["diagram_summary"] = "\n\n".join(summaries)

    # First pass: group and merge each type, building name maps for reference rewriting.
    merged_by_type: dict[str, list[dict]] = {}
    match_notes: list[str] = []
    for type_key in ENTITY_KEYS:
        entities = [e for p in per_doc_payloads for e in p.get(type_key, [])]
        uf, by_key, notes = _group_type(entities, type_key, reconcile_fn)
        match_notes.extend(notes)

        # component root -> member entities
        components: dict[str, list[dict]] = {}
        for ent in entities:
            key = _canon(ent.get("name", ""))
            if not key:
                continue
            components.setdefault(uf.find(key), []).append(ent)

        name_map: dict[str, str] = {}
        merged_entities: list[dict] = []
        for members in components.values():
            merged = _merge_entity(type_key, members)
            merged_entities.append(merged)
            for m in members:
                name_map[_canon(m.get("name", ""))] = merged["name"]
            if len({_canon(m.get("name", "")) for m in members}) > 1:
                merge_report.append(
                    {
                        "type": type_key,
                        "canonical": merged["name"],
                        "merged_from": sorted({m.get("name", "") for m in members}),
                        "sources": merged.get("_source", ""),
                        "method": "deterministic+claude",
                    }
                )
        merged_by_type[type_key] = merged_entities
        name_maps[type_key] = name_map

    # Second pass: rewrite cross-references to canonical names.
    _rewrite_references(merged_by_type, name_maps)
    for type_key in ENTITY_KEYS:
        merged_payload[type_key] = merged_by_type[type_key]

    # Open questions: union across docs, plus a note per conflict.
    questions: list[str] = []
    seen_q: set[str] = set()
    for p in per_doc_payloads:
        for q in p.get("open_questions", []):
            if q and q not in seen_q:
                seen_q.add(q)
                questions.append(q)
    for type_key in ENTITY_KEYS:
        for ent in merged_by_type[type_key]:
            if ent.get("_conflicts"):
                questions.append(
                    f"Conflicting values for {type_key[:-1]} '{ent['name']}': {ent['_conflicts']}"
                )
    questions.extend(match_notes)
    merged_payload["open_questions"] = questions

    graph = _build_graph(merged_by_type)
    return {"payload": merged_payload, "graph": graph, "merge_report": merge_report}


def _merge_entity(type_key: str, members: list[dict]) -> dict:
    """Union the fields of one component into a single entity with provenance."""
    name = _pick_canonical_name(members)
    merged: dict = {"name": name}

    conflicts: list[str] = []
    enum_fields = _SCALAR_ENUMS.get(type_key, [])
    ref_fields = _SCALAR_REFS.get(type_key, [])
    for field in enum_fields + ref_fields + ["alias", "description"]:
        if field not in members[0]:
            continue
        val, field_conflicts = _merge_field(members, field)
        merged[field] = val if val else ("unknown" if field in enum_fields else "")
        # Disagreeing parents/endpoints across documents is a real ambiguity, not noise.
        if field_conflicts and field in enum_fields + ref_fields:
            conflicts.append(f"{field}={field_conflicts}")

    # `level` is numeric; keep the mode-ish highest-confidence value as-is if present.
    if type_key == "capabilities":
        lvl, _ = _merge_field(members, "level")
        merged["level"] = members[0].get("level", 1) if not lvl else _coerce_level(members)

    for field in _LIST_FIELDS.get(type_key, []):
        values: list[str] = []
        for m in members:
            for v in m.get(field, []):
                if v and v not in values:
                    values.append(v)
        merged[field] = values

    # Evidence: accumulate distinct across members.
    evidences = []
    for m in members:
        ev = (m.get("evidence") or "").strip()
        if ev and ev not in evidences:
            evidences.append(ev)
    merged["evidence"] = " | ".join(evidences)

    # Confidence: max across members, bumped one level if corroborated by >1 source.
    sources = sorted({m.get("_source", "") for m in members if m.get("_source")})
    base_rank = max((_CONF_RANK.get(m.get("confidence", ""), 0) for m in members), default=1) or 1
    if len(sources) > 1:
        base_rank = min(3, base_rank + 1)
    merged["confidence"] = _RANK_CONF[base_rank]

    merged["_source"] = "; ".join(sources)
    # More than one reference only when the source itself holds duplicates of one name;
    # alfabet.py refuses to export such a row until a reviewer picks one.
    merged[_EXTERNAL_REF] = "; ".join(sorted({m[_EXTERNAL_REF] for m in members if m.get(_EXTERNAL_REF)}))
    surface_names = sorted({m.get("name", "") for m in members})
    merged["_provenance"] = "; ".join(surface_names) if len(surface_names) > 1 else ""
    merged["_conflicts"] = "; ".join(conflicts)
    return merged


def _coerce_level(members: list[dict]) -> int:
    best, best_rank = 1, -1
    for m in members:
        rank = _CONF_RANK.get(m.get("confidence", ""), 0)
        if rank > best_rank and m.get("level"):
            best, best_rank = m["level"], rank
    return best


def _rewrite_references(merged_by_type: dict[str, list[dict]], name_maps: dict[str, dict[str, str]]) -> None:
    """Point every cross-reference at the canonical name of the merged target."""

    def resolve(type_key: str, name: str) -> str:
        return name_maps.get(type_key, {}).get(_canon(name), name)

    for ent in merged_by_type.get("applications", []):
        for _src_key, ref_field, _rel in _REFERENCES["applications"]:
            ent[ref_field] = _dedupe([resolve(ref_field, n) for n in ent.get(ref_field, [])])

    for ent in merged_by_type.get("capabilities", []):
        if ent.get("parent"):
            ent["parent"] = resolve("capabilities", ent["parent"])

    for ent in merged_by_type.get("interfaces", []):
        if ent.get("provider"):
            ent["provider"] = resolve("applications", ent["provider"])
        if ent.get("consumer"):
            ent["consumer"] = resolve("applications", ent["consumer"])
        ent["data_objects"] = _dedupe([resolve("data_objects", n) for n in ent.get("data_objects", [])])


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

_TYPE_SINGULAR = {
    "applications": "application",
    "capabilities": "capability",
    "it_components": "it_component",
    "data_objects": "data_object",
    "interfaces": "interface",
}


def _build_graph(merged_by_type: dict[str, list[dict]]) -> dict:
    """Nodes = entities keyed by (type, name); edges from references, only between known nodes."""
    nodes: list[dict] = []
    node_ids: dict[str, set[str]] = {t: set() for t in ENTITY_KEYS}

    for type_key in ENTITY_KEYS:
        singular = _TYPE_SINGULAR[type_key]
        for ent in merged_by_type[type_key]:
            nid = f"{singular}:{ent['name']}"
            node_ids[type_key].add(ent["name"])
            nodes.append(
                {
                    "id": nid,
                    "type": singular,
                    "name": ent["name"],
                    "sources": ent.get("_source", ""),
                    "merged_from": ent.get("_provenance", ""),
                    "confidence": ent.get("confidence", ""),
                }
            )

    edges: list[dict] = []

    def add_edge(src_type, src_name, tgt_type, tgt_name, relation):
        if src_name in node_ids[src_type] and tgt_name in node_ids[tgt_type]:
            edges.append(
                {
                    "source": f"{_TYPE_SINGULAR[src_type]}:{src_name}",
                    "target": f"{_TYPE_SINGULAR[tgt_type]}:{tgt_name}",
                    "source_name": src_name,
                    "target_name": tgt_name,
                    "source_type": _TYPE_SINGULAR[src_type],
                    "target_type": _TYPE_SINGULAR[tgt_type],
                    "relation": relation,
                }
            )

    for ent in merged_by_type["applications"]:
        for src_key, ref_field, relation in _REFERENCES["applications"]:
            for target in ent.get(ref_field, []):
                add_edge("applications", ent["name"], ref_field, target, relation)

    for ent in merged_by_type["capabilities"]:
        if ent.get("parent"):
            add_edge("capabilities", ent["name"], "capabilities", ent["parent"], "child_of")

    for ent in merged_by_type["interfaces"]:
        if ent.get("provider"):
            add_edge("interfaces", ent["name"], "applications", ent["provider"], "provided_by")
        if ent.get("consumer"):
            add_edge("interfaces", ent["name"], "applications", ent["consumer"], "consumed_by")
        for do in ent.get("data_objects", []):
            add_edge("interfaces", ent["name"], "data_objects", do, "carries")

    return {"nodes": nodes, "edges": edges}
