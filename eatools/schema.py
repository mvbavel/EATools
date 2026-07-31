"""The extraction JSON schema and the enum vocabularies, in one place.

`extract.py` constrains the model to `EXTRACTION_SCHEMA`; `merge.py`, `leanix.py`, and
the frontend all speak the payload dict it produces. Enum lists are exported separately
so the frontend and CSV formatter can render the same Title-Case labels without
re-declaring them.

Structured-output note: Anthropic's structured outputs require ``additionalProperties:
false`` on every object and do not support string/number constraints (minLength, etc.).
Every property is listed in ``required`` — silent attributes come back as ``unknown`` or
an empty string, never omitted, so the model can't quietly drop a field.
"""

from __future__ import annotations

CONFIDENCE = ["high", "medium", "low"]
BUSINESS_CRITICALITY = [
    "mission_critical",
    "business_critical",
    "business_operational",
    "administrative_service",
    "unknown",
]
LIFECYCLE = ["plan", "phaseIn", "active", "phaseOut", "endOfLife", "unknown"]
HOSTING = ["cloud", "on_premise", "hybrid", "saas", "unknown"]
CAPABILITY_LEVEL = [1, 2, 3]
IT_COMPONENT_CATEGORY = [
    "software",
    "hardware",
    "service",
    "middleware",
    "database",
    "platform",
    "module",
    "unknown",
]
DATA_CLASSIFICATION = ["public", "internal", "confidential", "restricted", "unknown"]
INTEGRATION_TYPE = [
    "api",
    "file_transfer",
    "message_queue",
    "database_link",
    "etl",
    "manual",
    "unknown",
]
FREQUENCY = [
    "real_time",
    "near_real_time",
    "hourly",
    "daily",
    "weekly",
    "monthly",
    "on_demand",
    "unknown",
]


def _obj(properties: dict) -> dict:
    """Object schema with every property required and no extras allowed."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _enum(values, desc: str) -> dict:
    return {"enum": list(values), "description": desc}


def _str_list(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


_EVIDENCE = _str("The exact box text, connector label, or image region this came from.")
_CONFIDENCE = _enum(CONFIDENCE, "high=explicitly labelled; medium=read from shape/position/nesting; low=a guess worth reviewing.")

_APPLICATION = _obj(
    {
        "name": _str("Canonical application name."),
        "alias": _str("Short name/acronym, or empty."),
        "description": _str("What the application does, grounded in the diagram."),
        "business_criticality": _enum(BUSINESS_CRITICALITY, "Only if the diagram supports it, else unknown."),
        "lifecycle": _enum(LIFECYCLE, "Only if the diagram supports it, else unknown."),
        "hosting": _enum(HOSTING, "Only if the diagram supports it, else unknown."),
        "capabilities": _str_list("Names of capabilities this app realises (exact capability names)."),
        "data_objects": _str_list("Names of data objects this app owns/uses (exact data object names)."),
        "it_components": _str_list("Names of IT components this app runs on (exact component names)."),
        "evidence": _EVIDENCE,
        "confidence": _CONFIDENCE,
    }
)

_CAPABILITY = _obj(
    {
        "name": _str("Business capability name."),
        "description": _str("What the capability is."),
        "level": _enum(CAPABILITY_LEVEL, "1=top-level, 2=sub-capability, 3=leaf."),
        "parent": _str("Name of the parent capability, or empty for level 1."),
        "evidence": _EVIDENCE,
        "confidence": _CONFIDENCE,
    }
)

_IT_COMPONENT = _obj(
    {
        "name": _str("IT component / technology name."),
        "description": _str("What it is."),
        "category": _enum(IT_COMPONENT_CATEGORY, "Only if supported, else unknown."),
        "evidence": _EVIDENCE,
        "confidence": _CONFIDENCE,
    }
)

_DATA_OBJECT = _obj(
    {
        "name": _str("Data object / information entity name."),
        "description": _str("What data it represents."),
        "classification": _enum(DATA_CLASSIFICATION, "Only if supported, else unknown."),
        "evidence": _EVIDENCE,
        "confidence": _CONFIDENCE,
    }
)

_INTERFACE = _obj(
    {
        "name": _str("Interface / integration name."),
        "description": _str("What flows across it."),
        "provider": _str("Name of the providing application (exact name)."),
        "consumer": _str("Name of the consuming application (exact name)."),
        "data_objects": _str_list("Names of data objects carried (exact data object names)."),
        "integration_type": _enum(INTEGRATION_TYPE, "Only if supported, else unknown."),
        "frequency": _enum(FREQUENCY, "Only if supported, else unknown."),
        "evidence": _EVIDENCE,
        "confidence": _CONFIDENCE,
    }
)

EXTRACTION_SCHEMA = _obj(
    {
        "diagram_summary": _str("A few sentences describing what this diagram depicts."),
        "applications": {"type": "array", "items": _APPLICATION, "description": "Application fact sheets."},
        "capabilities": {"type": "array", "items": _CAPABILITY, "description": "Business capability fact sheets."},
        "it_components": {"type": "array", "items": _IT_COMPONENT, "description": "IT component fact sheets."},
        "data_objects": {"type": "array", "items": _DATA_OBJECT, "description": "Data object fact sheets."},
        "interfaces": {"type": "array", "items": _INTERFACE, "description": "Interface fact sheets."},
        "open_questions": _str_list("Genuine ambiguities a human reviewer should resolve."),
    }
)

# Entity-type keys carrying reviewable fact-sheet lists (excludes summary/questions).
ENTITY_KEYS = ["applications", "capabilities", "it_components", "data_objects", "interfaces"]
