"""Turn normalised diagrams into EA entities using Claude."""

from __future__ import annotations

import json
import os

import anthropic

from .model import SourceDoc

MODEL = "claude-opus-4-8"
MAX_TOKENS = 32000

CONFIDENCE = {"type": "string", "enum": ["high", "medium", "low"]}


def _obj(props: dict, *, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR = {"type": "string"}
_STRS = _arr(_STR)

SCHEMA = _obj({
    "diagram_summary": {
        "type": "string",
        "description": "Two or three sentences: what this architecture depicts and its scope.",
    },
    "applications": _arr(_obj({
        "name": _STR,
        "alias": {"type": "string", "description": "Acronym or short name as drawn, else empty."},
        "description": _STR,
        "business_criticality": {
            "type": "string",
            "enum": ["mission_critical", "business_critical", "business_operational",
                     "administrative_service", "unknown"],
        },
        "lifecycle": {
            "type": "string",
            "enum": ["plan", "phaseIn", "active", "phaseOut", "endOfLife", "unknown"],
        },
        "hosting": {"type": "string", "enum": ["cloud", "on_premise", "hybrid", "saas", "unknown"]},
        "capabilities": {**_STRS, "description": "Names of business capabilities this app supports."},
        "data_objects": {**_STRS, "description": "Names of data objects this app owns or masters."},
        "it_components": {**_STRS, "description": "Names of technologies/modules this app runs on."},
        "evidence": {"type": "string", "description": "The diagram label(s) or region this came from."},
        "confidence": CONFIDENCE,
    })),
    "capabilities": _arr(_obj({
        "name": _STR,
        "description": _STR,
        "level": {"type": "integer", "enum": [1, 2, 3]},
        "parent": {"type": "string", "description": "Parent capability name, empty if level 1."},
        "evidence": _STR,
        "confidence": CONFIDENCE,
    })),
    "it_components": _arr(_obj({
        "name": _STR,
        "description": _STR,
        "category": {
            "type": "string",
            "enum": ["software", "hardware", "service", "middleware", "database",
                     "platform", "module", "unknown"],
        },
        "evidence": _STR,
        "confidence": CONFIDENCE,
    })),
    "data_objects": _arr(_obj({
        "name": _STR,
        "description": _STR,
        "classification": {
            "type": "string",
            "enum": ["public", "internal", "confidential", "restricted", "unknown"],
        },
        "evidence": _STR,
        "confidence": CONFIDENCE,
    })),
    "interfaces": _arr(_obj({
        "name": _STR,
        "description": _STR,
        "provider": {"type": "string", "description": "Application name that provides/sends."},
        "consumer": {"type": "string", "description": "Application name that consumes/receives."},
        "data_objects": _STRS,
        "integration_type": {
            "type": "string",
            "enum": ["api", "file_transfer", "message_queue", "database_link",
                     "etl", "manual", "unknown"],
        },
        "frequency": {
            "type": "string",
            "enum": ["real_time", "near_real_time", "hourly", "daily", "weekly",
                     "monthly", "on_demand", "unknown"],
        },
        "evidence": _STR,
        "confidence": CONFIDENCE,
    })),
    "open_questions": {
        **_STRS,
        "description": "Ambiguities a human reviewer should resolve before import.",
    },
})

SYSTEM = """\
You are an enterprise architect extracting a structured inventory from IT \
architecture diagrams so it can be loaded into a LeanIX repository.

Classify what you find into the LeanIX fact sheet types:

- **Application** — a deployable system that delivers business function to users \
  (SAP S/4HANA, Salesforce, "Customer Portal"). Named products and bespoke systems.
- **Business Capability** — what the business *does*, not what software does it \
  ("Order Management", "Claims Handling"). Often drawn as grouping bands, swimlanes, \
  or container boxes around applications. Infer the hierarchy from nesting.
- **IT Component** — technology an application runs on or a sub-module inside one \
  (PostgreSQL, Kafka, Kubernetes, "Billing Engine" as a module of an ERP). If a box \
  is a part of a larger system rather than a system in its own right, it is an \
  IT Component, not an Application.
- **Data Object** — a business information entity that flows or is stored \
  ("Customer", "Invoice", "Policy"). Read these off connector labels and datastore shapes.
- **Interface** — a specific integration between two applications. Every meaningful \
  connector between two applications should become one, with provider and consumer \
  set to the direction of data flow.

Rules:

1. **Only record what the diagram supports.** Do not invent applications that are \
   not drawn, and do not enrich from your own product knowledge beyond a short \
   factual description. Where the diagram is silent on an attribute, use "unknown" \
   or an empty string — never guess a lifecycle or criticality.
2. **Deduplicate across pages and files.** The same system drawn on three slides is \
   one Application. Merge their attributes; prefer the most specific name and keep \
   the acronym as the alias.
3. **Every entity needs `evidence`** — quote the box label, connector label, or \
   describe the region of the image it came from, so a reviewer can verify it.
4. **Set `confidence` honestly.** `high` = explicitly labelled; `medium` = a \
   reasonable reading of shape/position/nesting; `low` = a guess worth reviewing.
5. **Cross-references use exact names.** An application's `capabilities` entries must \
   match `name` values in the capabilities array; the same for data objects, IT \
   components, and interface provider/consumer. Consistency here is what makes the \
   relations import work.
6. **Legend boxes, titles, footers, page numbers, and decorative shapes are not \
   entities.** Skip them.
7. Put anything genuinely ambiguous in `open_questions` rather than guessing.
"""


class ExtractionError(Exception):
    pass


def _client() -> anthropic.Anthropic:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ExtractionError(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY in the environment "
            "before starting the server."
        )
    return anthropic.Anthropic()


def build_content(docs: list[SourceDoc], extra_context: str = "") -> list[dict]:
    """Assemble the user turn: graph text for structured docs, images for visual ones."""
    content: list[dict] = []

    structured = [d for d in docs if d.kind == "structured"]
    visual = [d for d in docs if d.kind == "visual"]

    if structured:
        content.append({
            "type": "text",
            "text": (
                "The following diagrams were parsed into shape/connector graphs. "
                "Shape ids are internal — never use them as entity names.\n\n"
                + "\n\n---\n\n".join(d.render() for d in structured)
            ),
        })

    for doc in visual:
        label = doc.filename + (f" — {doc.notes}" if doc.notes else "")
        content.append({"type": "text", "text": f"Diagram image(s) from: {label}"})
        for img in doc.images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": img},
            })

    if extra_context.strip():
        content.append({
            "type": "text",
            "text": (
                "Additional context from the architect (authoritative — prefer it "
                f"over your own reading where they conflict):\n{extra_context.strip()}"
            ),
        })

    content.append({
        "type": "text",
        "text": "Extract the full EA inventory from the material above.",
    })
    return content


def extract(docs: list[SourceDoc], extra_context: str = "") -> dict:
    """Run extraction. Returns the parsed entity payload plus token usage."""
    if not docs:
        raise ExtractionError("No readable diagrams were supplied.")

    client = _client()

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        messages=[{"role": "user", "content": build_content(docs, extra_context)}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise ExtractionError("The model declined to process this content.")

    text = next((b.text for b in message.content if b.type == "text"), None)
    if not text:
        raise ExtractionError(
            f"No content returned (stop_reason={message.stop_reason}). "
            "If this was max_tokens, try fewer diagrams per run."
        )

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"Model returned malformed JSON: {exc}") from exc

    result["_usage"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
    }
    return result
