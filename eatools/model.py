"""The two data shapes every other module speaks in.

`SourceDoc` is a normalised diagram: either ``structured`` (a graph of shapes and
connectors, spread over pages) or ``visual`` (base64 PNGs for a vision model). The
``render()`` helpers flatten a structured graph into plain text a language model can
reason over — shapes listed with their container nesting, then connections as
``A -> B : label``.

The extracted *entity payload* is not modelled here; it is a plain dict matching the
JSON schema in ``extract.py``. Keeping it a dict means the review UI can post an edited
version straight back to ``leanix.py`` without a serialisation layer in between.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Shape:
    """A node in a structured diagram (a box, container, swimlane, ...)."""

    id: str
    text: str
    kind: str = ""
    # Container/grouping parent. Swimlanes and containers are capability hints, so the
    # nesting is preserved rather than flattened away during ingest.
    parent: str | None = None
    geometry: tuple[float, float, float, float] | None = None  # x, y, w, h


@dataclass
class Connector:
    """A directed edge between two shapes."""

    source: str | None
    target: str | None
    label: str = ""
    direction: str = "directed"  # "directed" | "bidirectional" | "undirected"


@dataclass
class Page:
    """One page/tab of a structured diagram."""

    shapes: list[Shape] = field(default_factory=list)
    connectors: list[Connector] = field(default_factory=list)
    name: str = ""

    def _shape_by_id(self) -> dict[str, Shape]:
        return {s.id: s for s in self.shapes}

    def _label(self, by_id: dict[str, Shape], shape_id: str | None) -> str:
        if shape_id is None:
            return "?"
        shape = by_id.get(shape_id)
        if shape is None:
            return shape_id
        return shape.text or shape.kind or shape.id

    def render(self) -> str:
        """Flatten this page to text: nested shapes, then connections."""
        by_id = self._shape_by_id()
        lines: list[str] = []
        if self.name:
            lines.append(f"## Page: {self.name}")

        lines.append("Shapes:")
        for shape in self.shapes:
            depth = 0
            parent = shape.parent
            seen: set[str] = set()
            # Walk the parent chain to indent by container nesting. `seen` guards against
            # a malformed diagram whose parent links form a cycle.
            while parent and parent in by_id and parent not in seen:
                seen.add(parent)
                depth += 1
                parent = by_id[parent].parent
            indent = "  " * depth
            text = shape.text.strip() or "(no label)"
            kind = f" [{shape.kind}]" if shape.kind else ""
            container = ""
            if shape.parent and shape.parent in by_id:
                container = f" (in: {self._label(by_id, shape.parent)})"
            lines.append(f"{indent}- {text}{kind}{container}")

        if self.connectors:
            lines.append("Connections:")
            arrow = {"bidirectional": "<->", "undirected": "--"}
            for conn in self.connectors:
                a = self._label(by_id, conn.source)
                b = self._label(by_id, conn.target)
                sep = arrow.get(conn.direction, "->")
                label = f" : {conn.label.strip()}" if conn.label.strip() else ""
                lines.append(f"  {a} {sep} {b}{label}")

        return "\n".join(lines)


@dataclass
class SourceDoc:
    """A normalised diagram ready for extraction.

    ``kind="structured"`` carries ``pages``; ``kind="visual"`` carries ``images``
    (base64-encoded PNG strings). ``source_name`` is the original filename and is the
    provenance key used when merging entities across documents.
    """

    kind: str  # "structured" | "visual"
    source_name: str
    pages: list[Page] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # base64 PNG data

    def render(self) -> str:
        """Flatten a structured document to text. Empty for visual docs."""
        if self.kind != "structured":
            return ""
        parts = [f"# Diagram: {self.source_name}"]
        for i, page in enumerate(self.pages, 1):
            if not page.name:
                page.name = f"{i}"
            parts.append(page.render())
        return "\n\n".join(parts)
