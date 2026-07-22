"""Normalised representations: diagram graph (input) and EA entities (output)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Input side: whatever the file was, it becomes one of these.
# ---------------------------------------------------------------------------


@dataclass
class Shape:
    id: str
    text: str
    kind: str = ""          # rectangle, cylinder, cloud, actor, ...
    parent: str | None = None   # container shape, if any -- often a grouping/zone
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0


@dataclass
class Connector:
    source: str | None
    target: str | None
    label: str = ""
    directed: bool = True


@dataclass
class Page:
    name: str
    shapes: list[Shape] = field(default_factory=list)
    connectors: list[Connector] = field(default_factory=list)

    def render(self) -> str:
        """Flatten the graph into text a model can reason over."""
        by_id = {s.id: s for s in self.shapes}
        lines = [f"## Page: {self.name}"]

        lines.append("\n### Shapes")
        for s in self.shapes:
            if not s.text.strip() and not s.kind:
                continue
            bits = [f"[{s.id}]"]
            if s.text.strip():
                bits.append(s.text.strip().replace("\n", " / "))
            if s.kind:
                bits.append(f"({s.kind})")
            if s.parent and s.parent in by_id:
                p = by_id[s.parent].text.strip().replace("\n", " ")
                if p:
                    bits.append(f"[inside: {p}]")
            lines.append("- " + " ".join(bits))

        if self.connectors:
            lines.append("\n### Connections")
            for c in self.connectors:
                src = by_id.get(c.source or "")
                tgt = by_id.get(c.target or "")
                sn = (src.text.strip().replace("\n", " ") if src else None) or c.source or "?"
                tn = (tgt.text.strip().replace("\n", " ") if tgt else None) or c.target or "?"
                arrow = "->" if c.directed else "--"
                lbl = f"  : {c.label.strip()}" if c.label.strip() else ""
                lines.append(f"- {sn} {arrow} {tn}{lbl}")

        return "\n".join(lines)


@dataclass
class SourceDoc:
    """One uploaded file, normalised."""

    filename: str
    kind: Literal["structured", "visual"]
    pages: list[Page] = field(default_factory=list)
    # base64-encoded PNGs, for visual sources (or rendered previews)
    images: list[str] = field(default_factory=list)
    notes: str = ""

    def render(self) -> str:
        head = f"# Source file: {self.filename}"
        if self.notes:
            head += f"\n({self.notes})"
        if not self.pages:
            return head
        return head + "\n\n" + "\n\n".join(p.render() for p in self.pages)


# ---------------------------------------------------------------------------
# Output side: the EA entities we extract.
# ---------------------------------------------------------------------------

# Every entity carries `evidence` -- what in the diagram led to this row.
# That is what makes the output reviewable rather than a black box.

ENTITY_TYPES = (
    "applications",
    "capabilities",
    "it_components",
    "data_objects",
    "interfaces",
)
