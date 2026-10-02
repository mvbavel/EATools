"""Consolidate per-document extractions into one picture via an in-memory graph.

Matching is a **reviewable proposal step**, then a deterministic merge:

1. `propose(per_doc_payloads, reconcile_fn)` gives every entity a stable ``_id`` and
   returns match proposals between entities of the same type:

   Names are compared as keys that ignore case, accents, spacing and punctuation; a
   system-of-record name is compared without its owner suffix ("(NTTD EMEAL DACH)").

   * ``exact`` -- same key (accepted by default unless several records share it);
   * ``alias`` -- an alias / short name equals another entity's name (accepted by
     default when unambiguous);
   * ``partial`` -- one name is contained word-for-word in the other (extra words, a
     diagram's own parenthetical);
   * ``fuzzy`` -- similar names (stdlib ``difflib``).

   Partial and fuzzy candidates are optionally judged by one Claude call per type
   (``ai`` = same/different); pending unless Claude confirms one and it is unambiguous.

   Two different system-of-record entities (different ``_alfabet_ref``) are never
   proposed as the same thing.

2. `merge(per_doc_payloads, accepted=[(id_a, id_b), ...])` merges exactly the accepted
   pairs: union attributes, accumulate evidence + provenance, bump confidence on
   corroboration. A pair that would join two system-of-record entities is refused and
   reported in ``blocked``. Without ``accepted``, the default-accepted proposals are used.

Everything is in-memory and per-request; the browser holds the per-document payloads and
the decisions and posts them back to re-merge. Nothing is written to disk.
"""

from __future__ import annotations

import difflib
import itertools
import re
import unicodedata

import anthropic

from .schema import ENTITY_KEYS

SIMILARITY_THRESHOLD = 0.82
# A partial (contained) name shorter than this anchors on too much: 'SAP', 'HR', 'CRM'.
PARTIAL_MIN_KEY = 4
# Partial candidates offered per entity; a generic name inside many record names would
# otherwise flood the Matches tab.
PARTIAL_LIMIT = 8
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


def _words(name: str) -> list[str]:
    """Casefolded, accent-folded alphanumeric runs: 'SAP S/4 HANA' -> ['sap', 's', '4', 'hana']."""
    folded = unicodedata.normalize("NFKD", (name or "").casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.findall(r"[^\W_]+", folded)


def _canon(name: str) -> str:
    """Matching key. Spacing and punctuation are dropped entirely because diagrams and
    Alfabet disagree on them ('SAP S/4 HANA' vs 'SAP S/4HANA'), never on the letters."""
    return "".join(_words(name))


def _base(name: str) -> str:
    """Name without a trailing parenthetical: Alfabet suffixes the owning org that way."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", name or "").strip()


def _within(short: str, words: list[str]) -> bool:
    """`short` (a key) occurs in the words, starting and ending on word boundaries -- so
    'saps4hana' is in 'SAP S/4HANA Finance' but 'data' is not in 'Metadata Manager'."""
    long_key = "".join(words)
    bounds = set(itertools.accumulate((len(w) for w in words), initial=0))
    start = long_key.find(short)
    while start != -1:
        if start in bounds and start + len(short) in bounds:
            return True
        start = long_key.find(short, start + 1)
    return False


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


def _match_name(ent: dict) -> str:
    """The name an entity is matched on. Alfabet suffixes the owning company --
    'SAP S/4HANA (NTTD EMEAL DACH)' -- which is not part of what the thing is called, so a
    record matches without it. A diagram's parenthetical may be 'legacy' or 'v2', so it is
    kept there (the partial step still compares without it)."""
    name = ent.get("name", "")
    return (_base(name) or name) if _is_record(ent) else name


def _key(ent: dict) -> str:
    return _canon(_match_name(ent))


def _node(ent: dict) -> str:
    """Candidate-comparison identity: one per record (two records sharing a name are still
    two things), one per name otherwise."""
    if _is_record(ent):
        return "r:" + (ent.get(_EXTERNAL_REF) or ent["_id"])
    return "n:" + _canon(ent.get("name", ""))


def _alt_names(ent: dict) -> set[str]:
    """Canonical alias + short names."""
    names = [ent.get("alias", "")] + list(ent.get("_match_names") or [])
    return {_canon(n) for n in names if _canon(n)}


def _variants(ent: dict) -> list[tuple[str, list[str]]]:
    """(key, words) for every name an entity may appear under, for partial/fuzzy checks.
    A record's owner suffix is left out so owner words never create a match."""
    name = _match_name(ent)
    raw = [name, _base(name), ent.get("alias", "")]
    raw += list(ent.get("_match_names") or [])
    out: dict[str, list[str]] = {}
    for name in raw:
        words = _words(name)
        if words:
            out.setdefault("".join(words), words)
    return list(out.items())


def _is_record(ent: dict) -> bool:
    """From the system of record: Alfabet apps carry a Reference, but Alfabet platforms and
    capabilities do not -- they are distinct records all the same."""
    return bool(ent.get(_EXTERNAL_REF)) or ent.get("_origin") == "alfabet"


def assign_ids(per_doc_payloads: list[dict]) -> None:
    """Stable per-request entity ids (type:doc:index); diagram origin unless already set."""
    for d, payload in enumerate(per_doc_payloads):
        for type_key in ENTITY_KEYS:
            for i, ent in enumerate(payload.get(type_key, [])):
                ent.setdefault("_id", f"{type_key}:{d}:{i}")
                ent.setdefault("_origin", "diagram")


class _Links:
    """Union-find over entity ids that refuses to join two different external records."""

    def __init__(self, entities: dict[str, dict]) -> None:
        self.uf = _UnionFind()
        self.refs: dict[str, set[str]] = {}
        for eid, ent in entities.items():
            self.uf.add(eid)
            if ent.get(_EXTERNAL_REF):
                self.refs[eid] = {ent[_EXTERNAL_REF]}

    def compatible(self, a: str, b: str) -> bool:
        sa, sb = self.refs.get(self.uf.find(a), set()), self.refs.get(self.uf.find(b), set())
        return not (sa and sb and sa != sb)

    def same(self, a: str, b: str) -> bool:
        return self.uf.find(a) == self.uf.find(b)

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.uf.find(a), self.uf.find(b)
        if ra == rb:
            return True
        if not self.compatible(a, b):
            return False
        merged = self.refs.pop(ra, set()) | self.refs.pop(rb, set())
        self.uf.union(ra, rb)
        if merged:
            self.refs[self.uf.find(rb)] = merged
        return True


def propose(per_doc_payloads: list[dict], reconcile_fn=None, company: str = "") -> dict:
    """Match proposals across all documents. Returns {proposals, notes}.

    ``company`` (e.g. "DACH") settles a diagram item that matches several Alfabet records
    equally well; without it the company is read from the diagram's file name.
    """
    assign_ids(per_doc_payloads)
    proposals: list[dict] = []
    notes: list[str] = []
    for type_key in ENTITY_KEYS:
        entities = [e for p in per_doc_payloads for e in p.get(type_key, []) if _canon(e.get("name", ""))]
        found, type_notes = _propose_type(type_key, entities, reconcile_fn, company)
        proposals.extend(found)
        notes.extend(type_notes)
    for i, prop in enumerate(proposals):
        prop["id"] = f"p{i}"
    return {"proposals": proposals, "notes": notes}


def _propose_type(type_key: str, entities: list[dict], reconcile_fn, company: str = "") -> tuple[list[dict], list[str]]:
    by_id = {e["_id"]: e for e in entities}
    # Tracks the default-accepted links so redundant proposals are not generated and
    # default acceptance never chains two external records together.
    links = _Links(by_id)
    out: list[dict] = []
    notes: list[str] = []
    proposed: set[frozenset] = set()
    # Node pairs already offered, so a later step does not repeat a pair via other members
    # of the same exact-name group.
    proposed_nodes: set[frozenset] = set()
    # Names that already match several records exactly: a Claude 'same' on yet another
    # candidate must not settle them.
    undecided: set[str] = set()
    # "Pick one" notes per diagram entity, emitted only if the company round cannot.
    pick_notes: dict[str, str] = {}

    def add(a: dict, b: dict, method: str, score: float, reason: str, accept: bool, ai: str | None = None) -> None:
        pair = frozenset((a["_id"], b["_id"]))
        if a["_id"] == b["_id"] or pair in proposed:
            return
        if _is_record(a) and _is_record(b) and not (a.get(_EXTERNAL_REF) and a.get(_EXTERNAL_REF) == b.get(_EXTERNAL_REF)):
            return  # two real records are never the same thing
        accept = accept and links.compatible(a["_id"], b["_id"])
        proposed.add(pair)
        proposed_nodes.add(frozenset((_node(a), _node(b))))
        out.append(
            {
                "type": type_key,
                "a": a["_id"],
                "b": b["_id"],
                "method": method,
                "score": round(score, 3),
                "ai": ai,
                "status": "accepted" if accept else "pending",
                "reason": reason,
            }
        )
        if accept:
            links.union(a["_id"], b["_id"])

    def same_name_reason(a: dict, b: dict, base: str) -> str:
        return base if _canon(a["name"]) == _canon(b["name"]) else base + " (owner suffix ignored)"

    # 1. Exact names (records without their owner suffix). A diagram that copies a
    # record's full name, suffix and all, joins that record's name too.
    full_to_key = {_canon(e["name"]): _key(e) for e in entities if _is_record(e)}
    key_of = {
        e["_id"]: _key(e) if _is_record(e) else full_to_key.get(_canon(e["name"]), _key(e))
        for e in entities
    }
    groups: dict[str, list[dict]] = {}
    for ent in entities:
        groups.setdefault(key_of[ent["_id"]], []).append(ent)
    for members in groups.values():
        if len(members) < 2:
            continue
        records: dict[str, list[dict]] = {}
        free: list[dict] = []
        for ent in members:
            (records.setdefault(_node(ent), []) if _is_record(ent) else free).append(ent)
        for copies in records.values():  # the same record seen twice
            for other in copies[1:]:
                add(copies[0], other, "exact", 1.0, "Same name and same Alfabet record", True)
        if len(records) > 1:
            # A diagram that spells out the owner picks its record.
            for ent in list(free):
                named = [c for c in records.values() if _canon(c[0]["name"]) == _canon(ent["name"])]
                if len(named) == 1:
                    add(named[0][0], ent, "exact", 1.0, "Same name, including the owner", True)
                    free.remove(ent)
            # Several records share the name (different owners, or true duplicates): offer
            # each candidate, decide nothing.
            for ent in free:
                undecided.add(_node(ent))
                for copies in records.values():
                    add(ent, copies[0], "exact", 1.0,
                        same_name_reason(ent, copies[0], "Same name as several Alfabet records"), False)
            if free:
                pick_notes[free[0]["_id"]] = (
                    f"'{free[0]['name']}' has the same name as {len(records)} separate Alfabet records "
                    f"({', '.join(sorted(c[0]['name'] for c in records.values()))}); pick the right one in Matches."
                )
            continue
        hub = next(iter(records.values()))[0] if records else members[0]
        for ent in members:
            if ent is not hub:
                add(hub, ent, "exact", 1.0, same_name_reason(hub, ent, "Same name"), True)

    # 2. Aliases and short names.
    claimants: dict[str, set[str]] = {}
    for ent in entities:
        if _is_record(ent):
            for alt in _alt_names(ent) | {_key(ent)}:
                claimants.setdefault(alt, set()).add(_node(ent))
    for ent in entities:
        own = key_of[ent["_id"]]
        for alt in _alt_names(ent):
            if alt == own:
                continue
            ambiguous = len(claimants.get(alt, ())) > 1
            for target in groups.get(alt, []):
                if links.same(ent["_id"], target["_id"]):
                    continue
                reason = f"'{target['name']}' is an alias / short name of '{ent['name']}'"
                if ambiguous:
                    reason += " -- but several Alfabet records share it"
                    undecided.add(_node(target if _is_record(ent) else ent))
                add(ent, target, "alias", 0.95, reason, not ambiguous)

    # 3. Partial and fuzzy names, with an optional Claude verdict; never accepted without
    # one. Only non-record names are compared (against everything): two system-of-record
    # names are never the same thing, and skipping them saves millions of pairs on a full
    # Alfabet import.
    rep: dict[str, dict] = {}
    for ent in entities:
        rep.setdefault(_node(ent), ent)
    keys = sorted(rep)
    anchored = {k for k in keys if _is_record(rep[k])}
    variants = {k: _variants(rep[k]) for k in keys}
    pairs: list[tuple[str, str, float, str]] = []
    matcher = difflib.SequenceMatcher(None)
    for a in keys:
        if a in anchored:
            continue
        partial: list[tuple[str, str, float, str]] = []
        for b in keys:
            if b == a or (b not in anchored and b < a) or frozenset((a, b)) in proposed_nodes:
                continue
            # Same key: already offered by the exact step.
            if key_of[rep[a]["_id"]] == key_of[rep[b]["_id"]] or links.same(rep[a]["_id"], rep[b]["_id"]):
                continue
            found = _compare(variants[a], variants[b], matcher)
            if found:
                method, score = found
                (partial if method == "partial" else pairs).append((a, b, score, method))
        partial.sort(key=lambda p: -p[2])
        if len(partial) > PARTIAL_LIMIT:
            notes.append(
                f"'{rep[a]['name']}' appears inside {len(partial)} {type_key} names; only the "
                f"{PARTIAL_LIMIT} closest are offered in Matches."
            )
        pairs.extend(partial[:PARTIAL_LIMIT])

    verdict: dict[frozenset, str] = {}
    if pairs and reconcile_fn is not None:
        involved = sorted({k for a, b, _, _ in pairs for k in (a, b)})
        candidates = [
            {
                "name": rep[k].get("name", ""),
                # Alfabet descriptions can run to pages; the name carries the identity.
                "description": (rep[k].get("description") or "")[:300],
                "sources": rep[k].get("_source", ""),
            }
            for k in involved
        ]
        # Claude answers in names; records sharing a full name map to every such record,
        # which the record_hits check below then treats as ambiguous.
        nodes_named: dict[str, list[str]] = {}
        for k in involved:
            nodes_named.setdefault(_canon(rep[k].get("name", "")), []).append(k)
        group_of: dict[str, int] = {}
        for gi, member_names in enumerate(reconcile_fn(type_key, candidates)):
            for name in member_names:
                for k in nodes_named.get(_canon(name), []):
                    group_of[k] = gi
        for a, b, _, _ in pairs:
            same = a in group_of and group_of.get(a) == group_of.get(b)
            verdict[frozenset((a, b))] = "same" if same else "different"
        # Claude may group names the similarity pass did not pair; offer those too.
        by_group: dict[int, list[str]] = {}
        for key, gi in group_of.items():
            by_group.setdefault(gi, []).append(key)
        paired = {frozenset((a, b)) for a, b, _, _ in pairs}
        for members in by_group.values():
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    if frozenset((a, b)) not in paired and not (a in anchored and b in anchored):
                        score = difflib.SequenceMatcher(None, _key(rep[a]), _key(rep[b])).ratio()
                        pairs.append((a, b, score, "fuzzy"))
                        verdict[frozenset((a, b))] = "same"

    # A name Claude ties to several separate records is ambiguous: offer, don't accept.
    record_hits: dict[str, set[str]] = {}
    for a, b, _, _ in pairs:
        if verdict.get(frozenset((a, b))) == "same":
            for x, y in ((a, b), (b, a)):
                if y in anchored:
                    record_hits.setdefault(x, set()).add(y)
    for key, refs in record_hits.items():
        if len(refs) > 1:
            names = [rep[key]["name"]] + sorted(rep[k]["name"] for k in refs)
            pick_notes[rep[key]["_id"]] = (
                f"Possible match among {type_key}: {', '.join(names)} -- several are separate "
                "Alfabet records, so nothing was merged; pick one in Matches."
            )

    for a, b, score, method in sorted(pairs, key=lambda p: -p[2]):
        ai = verdict.get(frozenset((a, b)))
        ambiguous = len(record_hits.get(a, ())) > 1 or len(record_hits.get(b, ())) > 1
        ambiguous = ambiguous or a in undecided or b in undecided
        if method == "partial":
            reason = "One name contains the other (extra words)"
        else:
            reason = f"Similar names ({score:.0%})"
        if ai:
            reason += f"; Claude judged them {'the same' if ai == 'same' else 'different'}"
        add(rep[a], rep[b], method, score, reason, ai == "same" and not ambiguous, ai)

    settled = _company_round(out, by_id, links, company, notes)
    notes.extend(n for eid, n in pick_notes.items() if eid not in settled)
    return out, notes


def _owner(name: str) -> list[str]:
    """Words of a record's owner suffix: 'X (NTTD EMEAL DACH)' -> ['nttd', 'emeal', 'dach']."""
    m = re.search(r"\(([^()]*)\)\s*$", name or "")
    return _words(m.group(1)) if m else []


def _file_words(ent: dict) -> set[str]:
    """Words of the file names an entity was extracted from, without extensions."""
    stems = (re.sub(r"\.[A-Za-z0-9]{1,5}$", "", src.strip()) for src in (ent.get("_source") or "").split(";"))
    return {w for stem in stems for w in _words(stem)}


# Shorter file-name words ('S', 'CN', 'v7') say too little to decide an owner.
_FILE_OWNER_MIN = 3


def _company_round(out: list[dict], by_id: dict[str, dict], links: _Links, company: str, notes: list[str]) -> set[str]:
    """Second round for a diagram item matching several Alfabet records equally well (same
    name, alias, or Claude 'same'): keep only the records whose owner suffix is the company.
    One left: accept it and reject the rest. Several: reject the others, leave those
    pending. None: change nothing. Returns the ids of the diagram items settled.

    The company is ``company`` when given, else the diagram's file name -- using only words
    that tell the candidate owners apart, so 'NTT' in a file name decides nothing when
    every candidate is NTT-owned."""
    by_item: dict[str, list[dict]] = {}
    for p in out:
        a, b = by_id[p["a"]], by_id[p["b"]]
        if p["status"] != "pending" or _is_record(a) == _is_record(b):
            continue
        item = a if not _is_record(a) else b
        by_item.setdefault(item["_id"], []).append(p)

    def record_of(p: dict) -> dict:
        return by_id[p["b"]] if _is_record(by_id[p["b"]]) else by_id[p["a"]]

    wanted = _canon(company)
    settled: set[str] = set()
    for item_id, props in by_item.items():
        strong = [p for p in props if p["method"] in ("exact", "alias") or p["ai"] == "same"]
        if len({record_of(p)["_id"] for p in strong}) < 2:
            continue
        item = by_id[item_id]
        owner = {id(p): _owner(record_of(p)["name"]) for p in strong}
        if wanted:
            hits = [p for p in strong if _within(wanted, owner[id(p)])]
            label = f"company '{company.strip()}'"
        else:
            shared = set.intersection(*(set(w) for w in owner.values()))
            clues = {
                w for w in _file_words(item)
                if len(w) >= _FILE_OWNER_MIN and w not in shared and any(w in o for o in owner.values())
            }
            if not clues:
                continue
            hits = [p for p in strong if clues & set(owner[id(p)])]
            label = f"company '{' '.join(sorted(clues)).upper()}' from the file name"
        if not hits:
            if wanted:
                notes.append(
                    f"None of the {len(strong)} Alfabet records matching '{item['name']}' belongs to "
                    f"{label}; pick one in Matches."
                )
            continue
        if len(hits) == len(strong):
            continue
        if len(hits) == 1:
            pick = hits[0]
            if links.union(pick["a"], pick["b"]):
                pick["status"] = "accepted"
                pick["reason"] += f"; owner matches {label}"
                for p in props:
                    if p is not pick:
                        p["status"] = "rejected"
                        p["reason"] += f"; owner is not {label}"
                settled.add(item_id)
            continue
        for p in strong:
            if p not in hits:
                p["status"] = "rejected"
                p["reason"] += f"; owner is not {label}"
        for p in hits:
            p["reason"] += f"; {len(hits)} candidates owned by {label}"
    return settled


def _compare(va, vb, matcher: difflib.SequenceMatcher) -> tuple[str, float] | None:
    """Best relation between two entities' name variants: ``partial`` when one name (or its
    org-less form) is contained word-for-word in the other, else ``fuzzy`` when similar.
    Scores stay below 1 so they never read as an exact match."""
    best = 0.0
    is_partial = False
    for ka, wa in va:
        matcher.set_seq2(ka)
        for kb, wb in vb:
            matcher.set_seq1(kb)
            if ka == kb or (len(ka) >= PARTIAL_MIN_KEY and _within(ka, wb)) or (
                len(kb) >= PARTIAL_MIN_KEY and _within(kb, wa)
            ):
                is_partial = True
                best = max(best, matcher.ratio())
                continue
            # The quick upper bounds skip the full ratio for almost every pair.
            floor = max(best, SIMILARITY_THRESHOLD)
            if matcher.real_quick_ratio() >= floor and matcher.quick_ratio() >= floor:
                best = max(best, matcher.ratio())
    if is_partial:
        return "partial", min(best, 0.99)
    if best >= SIMILARITY_THRESHOLD:
        return "fuzzy", min(best, 0.99)
    return None


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


def merge(
    per_doc_payloads: list[dict],
    client: anthropic.Anthropic | None = None,
    reconcile_fn=None,
    accepted: list[tuple[str, str]] | None = None,
    company: str = "",
):
    """Consolidate per-document payloads. Returns {payload, graph, merge_report, blocked}.

    With ``accepted`` (pairs of entity ids) exactly those pairs are merged -- the review
    path. Without it, matches are proposed and the default-accepted ones are used.
    """
    notes: list[str] = []
    if accepted is None:
        if reconcile_fn is None and client is not None:
            reconcile_fn = _llm_reconcile(client)
        proposed = propose(per_doc_payloads, reconcile_fn, company)
        accepted = [(p["a"], p["b"]) for p in proposed["proposals"] if p["status"] == "accepted"]
        notes = proposed["notes"]
    else:
        assign_ids(per_doc_payloads)

    result = _merge_accepted(per_doc_payloads, accepted)
    result["payload"]["open_questions"].extend(notes)
    return result


def propose_and_merge(
    per_doc_payloads: list[dict], client: anthropic.Anthropic | None = None, company: str = ""
) -> dict:
    """Proposals plus the merge of their defaults: what the review UI starts from."""
    proposed = propose(per_doc_payloads, _llm_reconcile(client) if client is not None else None, company)
    accepted = [(p["a"], p["b"]) for p in proposed["proposals"] if p["status"] == "accepted"]
    merged = _merge_accepted(per_doc_payloads, accepted)
    merged["proposals"] = proposed["proposals"]
    merged["notes"] = proposed["notes"]
    return merged


def _merge_accepted(per_doc_payloads: list[dict], accepted) -> dict:
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

    entities = {
        e["_id"]: e
        for p in per_doc_payloads
        for t in ENTITY_KEYS
        for e in p.get(t, [])
        if _canon(e.get("name", ""))
    }
    type_of = {e["_id"]: t for p in per_doc_payloads for t in ENTITY_KEYS for e in p.get(t, [])}
    links = _Links(entities)
    blocked: list[list[str]] = []
    for pair in accepted or []:
        a, b = (list(pair) + ["", ""])[:2]
        if a not in entities or b not in entities or type_of[a] != type_of[b]:
            continue
        if not links.union(a, b):
            blocked.append([a, b])

    merged_by_type: dict[str, list[dict]] = {}
    for type_key in ENTITY_KEYS:
        components: dict[str, list[dict]] = {}
        for p in per_doc_payloads:
            for ent in p.get(type_key, []):
                if ent.get("_id") in entities:
                    components.setdefault(links.uf.find(ent["_id"]), []).append(ent)

        name_map: dict[str, str] = {}
        merged_entities: list[dict] = []
        for members in components.values():
            merged = _merge_entity(type_key, members)
            merged["_id"] = members[0]["_id"]
            merged["_members"] = [m["_id"] for m in members]
            merged_entities.append(merged)
            for m in members:
                # First wins: a rejected exact match leaves two entities with one name.
                name_map.setdefault(_canon(m.get("name", "")), merged["name"])
            if len({_canon(m.get("name", "")) for m in members}) > 1:
                merge_report.append(
                    {
                        "type": type_key,
                        "canonical": merged["name"],
                        "merged_from": sorted({m.get("name", "") for m in members}),
                        "sources": merged.get("_source", ""),
                        "method": "accepted match",
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
    merged_payload["open_questions"] = questions

    graph = _build_graph(merged_by_type)
    return {"payload": merged_payload, "graph": graph, "merge_report": merge_report, "blocked": blocked}


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
    merged["_origin"] = "; ".join(sorted({m.get("_origin", "diagram") for m in members}))
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
    seen_ids: set[str] = set()

    for type_key in ENTITY_KEYS:
        singular = _TYPE_SINGULAR[type_key]
        for ent in merged_by_type[type_key]:
            nid = f"{singular}:{ent['name']}"
            if nid in seen_ids:  # a rejected exact match keeps two entities of one name
                nid = f"{nid}#{ent.get('_id', len(nodes))}"
            seen_ids.add(nid)
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
