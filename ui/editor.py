"""Pure helpers for editing a workflow file draft (no Streamlit, unit-tested).

A draft is the ``.graph.json`` content as a dict (see docs/guides/workflows.md):
``{"description", "input", "nodes", "edges", "output"}``. The UI edits it in
session state and sends it to the API to validate / save; the server is the
authority on what's valid.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

NODE_TYPES = ("llm", "tool", "subagent", "branch", "human_review")
FIELD_TYPES = ("string", "number", "integer", "boolean", "array", "object")
BRANCH_OPS = (
    "eq", "ne", "lt", "le", "gt", "ge", "in", "not_in",
    "contains", "exists", "not_exists", "truthy", "falsy",
)  # fmt: skip
TYPE_LABELS = {
    "llm": "🧠 LLM call",
    "tool": "🔧 Tool",
    "subagent": "🤖 Subagent",
    "branch": "🔀 Branch",
    "human_review": "🙋 Human review",
}
END = "END"
START = "START"
_SAFE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def node_template(node_type: str) -> dict[str, Any]:
    """A new node of *node_type* with sensible starting values."""
    templates: dict[str, dict[str, Any]] = {
        "llm": {"type": "llm", "prompt": "Summarise: {{input.text}}"},
        "tool": {"type": "tool", "tool": "", "args": {}},
        "subagent": {"type": "subagent", "subagent": "", "prompt": ""},
        "branch": {"type": "branch", "rules": [], "else": END},
        "human_review": {"type": "human_review", "message": "Does this look right?"},
    }
    return copy.deepcopy(templates[node_type])


TEMPLATES: dict[str, dict[str, Any]] = {
    "Blank (one LLM step)": {
        "description": "What this workflow does — the agent reads this to decide when to run it.",
        "input": {"text": {"type": "string", "description": "The text to work on."}},
        "nodes": {"answer": {"type": "llm", "prompt": "Summarise:\n{{input.text}}"}},
        "edges": [{"from": START, "to": "answer"}],
        "output": "answer",
    },
    "Draft → human review → deliver": {
        "description": "Draft something, pause for approval, then return the approved text.",
        "input": {"topic": {"type": "string", "description": "What to write about."}},
        "nodes": {
            "draft": {
                "type": "llm",
                "prompt": "Write a short, clear note about: {{input.topic}}",
                "output": {"text": {"type": "string"}},
            },
            "review": {
                "type": "human_review",
                "message": "Approve this draft?",
                "show": ["draft"],
                "editable": "draft",
            },
        },
        "edges": [{"from": START, "to": "draft"}, {"from": "draft", "to": "review"}],
        "output": "draft.text",
    },
}


def new_draft(template: str) -> dict[str, Any]:
    return copy.deepcopy(TEMPLATES[template])


def valid_id(node_id: str) -> bool:
    return bool(_SAFE.match(node_id or "")) and node_id not in (START, END, "input")


def add_node(draft: dict[str, Any], node_id: str, node_type: str) -> dict[str, Any]:
    """Return *draft* with a new node (and, if it's the first, an edge from START).

    Raises:
        ValueError: bad or duplicate id, or unknown type.
    """
    if not valid_id(node_id):
        raise ValueError("Node ids are snake_case: a letter, then letters, digits or _.")
    if node_id in draft.get("nodes", {}):
        raise ValueError(f"There is already a node {node_id!r}.")
    if node_type not in NODE_TYPES:
        raise ValueError(f"Unknown node type {node_type!r}.")
    out = copy.deepcopy(draft)
    out.setdefault("nodes", {})[node_id] = node_template(node_type)
    if not any(_sources(e) == [START] for e in out.setdefault("edges", [])):
        out["edges"].append({"from": START, "to": node_id})
    return out


def remove_node(draft: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Return *draft* without *node_id*: its edges go, and routes to it now go to END."""
    out = copy.deepcopy(draft)
    out.get("nodes", {}).pop(node_id, None)
    edges = []
    for edge in out.get("edges", []):
        sources = [s for s in _sources(edge) if s != node_id]
        if not sources or edge["to"] == node_id:
            continue
        edges.append({"from": sources[0] if len(sources) == 1 else sources, "to": edge["to"]})
    out["edges"] = edges
    for node in out.get("nodes", {}).values():
        if node.get("type") == "branch":
            node["rules"] = [r for r in node.get("rules", []) if r.get("then") != node_id]
            if node.get("else") == node_id:
                node["else"] = END
        if node.get("type") == "human_review" and node.get("on_reject") == node_id:
            node["on_reject"] = END
    if (out.get("output") or "").split(".", 1)[0] == node_id:
        out["output"] = ""
    return out


def _sources(edge: dict[str, Any]) -> list[str]:
    src = edge.get("from")
    return list(src) if isinstance(src, list) else [src]


def edges_to_rows(draft: dict[str, Any]) -> list[dict[str, str]]:
    """Edges as table rows; several sources (wait for all) are comma-separated."""
    return [
        {"from": ", ".join(_sources(e)), "to": str(e.get("to", ""))} for e in draft.get("edges", [])
    ]


def rows_to_edges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Table rows back to edges (blank rows are dropped)."""
    edges = []
    for row in rows:
        sources = [s.strip() for s in str(row.get("from") or "").split(",") if s.strip()]
        target = str(row.get("to") or "").strip()
        if not sources or not target:
            continue
        edges.append({"from": sources[0] if len(sources) == 1 else sources, "to": target})
    return edges


def input_to_rows(draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "type": spec.get("type", "string"),
            "description": spec.get("description", ""),
            "required": spec.get("required", True),
        }
        for name, spec in (draft.get("input") or {}).items()
    ]


def rows_to_input(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        spec: dict[str, Any] = {"type": row.get("type") or "string"}
        if row.get("description"):
            spec["description"] = str(row["description"])
        if row.get("required") is False:
            spec["required"] = False
        fields[name] = spec
    return fields


def output_to_rows(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """An ``llm`` node's structured output fields as table rows."""
    return input_to_rows({"input": fields})


def targets(draft: dict[str, Any]) -> list[str]:
    """Where an edge / route can go: every node, plus END."""
    return [*draft.get("nodes", {}), END]


def parse_json(text: str, what: str) -> Any:
    """Decode JSON typed into a form, with a readable error."""
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} is not valid JSON: {exc.msg} (line {exc.lineno}).") from exc


def coerce_input(fields: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Turn form strings into typed run input using the workflow's input fields."""
    out: dict[str, Any] = {}
    for name, spec in fields.items():
        raw = values.get(name, "")
        if raw == "" and not spec.get("required", True):
            continue
        kind = spec.get("type", "string")
        if kind == "string":
            out[name] = raw
        elif kind == "integer":
            out[name] = int(raw)
        elif kind == "number":
            out[name] = float(raw)
        elif kind == "boolean":
            out[name] = str(raw).lower() in ("1", "true", "yes", "on")
        else:
            out[name] = parse_json(raw, name)
    return out


def graph_diff(old: dict[str, Any], new: dict[str, Any]) -> str:
    """A unified diff between two workflow files ("" when identical)."""
    import difflib

    def lines(graph: dict[str, Any]) -> list[str]:
        return json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True).splitlines()

    return "\n".join(
        difflib.unified_diff(lines(old), lines(new), "this version", "current", lineterm="", n=2)
    )


def mermaid_html(code: str, *, height: int = 480) -> str:
    """A self-contained HTML snippet that renders a Mermaid diagram."""
    escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""
<div class="mermaid" style="display:flex;justify-content:center">{escaped}</div>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  mermaid.initialize({{ startOnLoad: true, theme: dark ? "dark" : "default" }});
</script>
<style>body{{margin:0;font-family:sans-serif}} .mermaid svg{{max-height:{height - 20}px}}</style>
"""
