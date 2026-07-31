"""One schema-constrained Claude call per document.

`extract(source_doc, context, client)` builds a single user turn — rendered graph text
for structured docs, image blocks for visual docs, plus optional architect context — and
returns `(payload_dict, usage)`. The payload matches `EXTRACTION_SCHEMA`; cross-document
consolidation is `merge.py`'s job, so the system prompt only asks the model to dedupe
*within* this one document.

SDK errors are mapped to short human messages and re-raised as `ExtractionError`
**with `from None`** so no request internals (keys, headers, URLs) reach the browser.
"""

from __future__ import annotations

import anthropic

from .model import SourceDoc
from .schema import EXTRACTION_SCHEMA

MODEL = "claude-opus-4-8"
MAX_TOKENS = 32000

SYSTEM_PROMPT = """\
You are a senior enterprise architect. You read an IT architecture diagram and map what \
it depicts onto five LeanIX fact-sheet types: Applications, Business Capabilities, IT \
Components, Data Objects, and Interfaces.

Your output is REVIEWED by a human before import, so it must be honest and traceable, not \
impressive. Follow these rules exactly:

1. Only record what the diagram supports. Do NOT invent entities, and do NOT enrich \
   lifecycle, criticality, hosting, classification, or ownership from your own product \
   knowledge. If an attribute is not shown, return "unknown" (for enums) or "" (for text) \
   — never a guess dressed up as fact.
2. Deduplicate the same system within this document: if a box appears on several pages, \
   record it once.
3. Every entity MUST carry `evidence`: the box text, connector label, or image region it \
   came from. No evidence means you should not be recording it.
4. Set `confidence` honestly: high = explicitly labelled in the diagram; medium = a \
   reasonable reading of shape, position, or container nesting; low = a guess worth a \
   reviewer's attention.
5. Cross-references between entities MUST use the exact `name` value of the referenced \
   entity, so relation import works. If application "Billing" runs on component \
   "PostgreSQL", the app's it_components list contains exactly "PostgreSQL".
6. Skip legends, titles, footers, page numbers, and purely decorative shapes.
7. When something is genuinely ambiguous, put it in `open_questions` instead of guessing.
"""


class ExtractionError(Exception):
    """A user-safe extraction failure. Its message contains no request internals."""


_ERROR_MESSAGES = {
    anthropic.AuthenticationError: "Authentication failed — check the API key.",
    anthropic.PermissionDeniedError: "The API key lacks permission for this model.",
    anthropic.RateLimitError: "Rate limited by the model API — please retry shortly.",
    anthropic.APIConnectionError: "Could not reach the model API — check connectivity.",
}


def _content_blocks(source: SourceDoc, context: str) -> list[dict]:
    """Build the user-turn content: architect context, then the diagram."""
    blocks: list[dict] = []
    if context and context.strip():
        blocks.append({"type": "text", "text": f"Architect-provided context:\n{context.strip()}"})

    if source.kind == "structured":
        blocks.append(
            {
                "type": "text",
                "text": (
                    "Extract enterprise-architecture entities from this diagram, "
                    "rendered as text (shapes with their container nesting, then "
                    f"connections as `A -> B : label`):\n\n{source.render()}"
                ),
            }
        )
    else:  # visual
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"Extract enterprise-architecture entities from this diagram "
                    f"({len(source.images)} page image(s), from '{source.source_name}')."
                ),
            }
        )
        for img in source.images:
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img},
                }
            )
    return blocks


def _tag_sources(payload: dict, source_name: str) -> dict:
    """Stamp every entity with the document it came from (server controls the name)."""
    from .schema import ENTITY_KEYS

    for key in ENTITY_KEYS:
        for entity in payload.get(key, []):
            entity["_source"] = source_name
    return payload


def extract(source: SourceDoc, context: str, client: anthropic.Anthropic) -> tuple[dict, dict]:
    """Run one extraction. Returns (payload dict tagged with _source, usage dict)."""
    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
            },
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": _content_blocks(source, context)}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        # Covers AuthenticationError/PermissionDeniedError/RateLimitError (all subclasses)
        # plus any other non-2xx. Map known types, fall back to a generic message.
        for exc_type, msg in _ERROR_MESSAGES.items():
            if isinstance(exc, exc_type):
                raise ExtractionError(msg) from None
        raise ExtractionError("The model API returned an error.") from None
    except anthropic.APIConnectionError:
        raise ExtractionError(_ERROR_MESSAGES[anthropic.APIConnectionError]) from None

    import json

    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise ExtractionError("The model returned output that could not be parsed.") from None

    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
    }
    return _tag_sources(payload, source.source_name), usage
