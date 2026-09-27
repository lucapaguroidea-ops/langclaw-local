"""
Graph workflow files — a LangGraph ``StateGraph`` written down as data.

A graph workflow is a JSON file, ``workflows/<name>.graph.json``, that the UI or
the agent can edit and that :func:`langclaw.workflows.graph.compile.build_state_graph`
turns into a real LangGraph ``StateGraph`` (checkpointed, interruptible). The file
is the source of truth: readable, diffable, and validated before it is loaded.

Shape::

    {
      "description": "Classify an uploaded document and file it.",
      "input": {"key": {"type": "string", "description": "Bucket object key"}},
      "nodes": {
        "fetch":    {"type": "tool", "tool": "bucket_read", "args": {"key": "{{input.key}}"}},
        "classify": {"type": "llm", "prompt": "Who sent this?\\n{{fetch}}",
                     "output": {"sender": {"type": "string"},
                                "confidence": {"type": "number"}}},
        "check":    {"type": "branch",
                     "rules": [{"if": {"path": "classify.confidence", "op": "lt", "value": 0.8},
                                "then": "review"}],
                     "else": "save"},
        "review":   {"type": "human_review", "message": "Is this right?",
                     "show": ["classify"], "editable": "classify"},
        "save":     {"type": "tool", "tool": "documents_insert", "args": {"meta": "{{classify}}"}}
      },
      "edges": [
        {"from": "START", "to": "fetch"},
        {"from": "fetch", "to": "classify"},
        {"from": "classify", "to": "check"},
        {"from": "review", "to": "save"}
      ],
      "output": "classify"
    }

State model: ``input`` holds the run input; every node's result is stored under
its ``save_as`` key (default: the node id). Templates (``{{input.key}}``,
``{{classify.sender}}``) read from that namespace. A node with no outgoing edge
ends the run. ``branch`` nodes route by rules instead of edges; a
``human_review`` node pauses the run (LangGraph ``interrupt``) until someone
approves, edits, or rejects.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: Node ids and ``save_as`` keys: snake_case, usable as template roots.
_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
#: Reserved names that cannot be node ids or ``save_as`` keys.
_RESERVED = frozenset({"START", "END", "input", "__start__", "__end__"})
#: A ``{{ path }}`` template placeholder.
TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}")

#: Field types an ``llm`` node's structured ``output`` (and ``input``) may declare.
FIELD_TYPES = ("string", "number", "integer", "boolean", "array", "object")
#: Comparison operators a ``branch`` rule may use.
BRANCH_OPS = (
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "in",
    "not_in",
    "contains",
    "exists",
    "not_exists",
    "truthy",
    "falsy",
)

START = "START"
END = "END"


class GraphSpecError(ValueError):
    """A graph workflow file is invalid. ``errors`` lists every problem found."""

    def __init__(self, name: str, errors: list[str]) -> None:
        self.name = name
        self.errors = errors
        bullet = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"Graph workflow {name!r} is invalid:\n{bullet}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Strict):
    """One typed field of a node's structured output or the run input."""

    type: Literal["string", "number", "integer", "boolean", "array", "object"] = "string"
    description: str = ""
    required: bool = True


class _NodeBase(_Strict):
    label: str = ""
    """Human-facing name shown in the UI (defaults to the node id)."""
    save_as: str = ""
    """State key the node's result is stored under (default: the node id)."""


class LlmNode(_NodeBase):
    """One model call — no tools, no agent loop."""

    type: Literal["llm"]
    prompt: str
    system: str = ""
    model: str = ""
    """Model override (e.g. ``openai:gpt-4.1``); empty ⇒ the agent's default model."""
    output: dict[str, FieldSpec] = Field(default_factory=dict)
    """Structured output fields; empty ⇒ plain text."""


class ToolNode(_NodeBase):
    """Call one registered tool with (templated) arguments."""

    type: Literal["tool"]
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class SubagentNode(_NodeBase):
    """Delegate to a registered subagent (its own tools and context window)."""

    type: Literal["subagent"]
    subagent: str
    prompt: str


class BranchCondition(_Strict):
    path: str
    op: Literal[
        "eq",
        "ne",
        "lt",
        "le",
        "gt",
        "ge",
        "in",
        "not_in",
        "contains",
        "exists",
        "not_exists",
        "truthy",
        "falsy",
    ] = "truthy"
    value: Any = None


class BranchRule(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    if_: BranchCondition = Field(alias="if")
    then: str


class BranchNode(_NodeBase):
    """Route to the first rule whose condition holds, else to ``else``."""

    type: Literal["branch"]
    rules: list[BranchRule] = Field(default_factory=list)
    else_: str = Field(default=END, alias="else")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HumanReviewNode(_NodeBase):
    """Pause the run until a person approves, edits, or rejects."""

    type: Literal["human_review"]
    message: str
    show: list[str] = Field(default_factory=list)
    """State keys shown to the reviewer (default: everything produced so far)."""
    editable: str = ""
    """State key the reviewer may correct; an ``edit`` decision replaces its fields."""
    on_reject: str = END
    """Where a rejected run goes (default: end the run)."""


Node = Annotated[
    LlmNode | ToolNode | SubagentNode | BranchNode | HumanReviewNode,
    Field(discriminator="type"),
]


class Edge(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str | list[str] = Field(alias="from")
    """Source node, ``START``, or a list of nodes that must *all* finish first."""
    to: str

    @property
    def sources(self) -> list[str]:
        return [self.from_] if isinstance(self.from_, str) else list(self.from_)


class GraphSpec(_Strict):
    """A parsed graph workflow file (the name comes from the filename)."""

    name: str = ""
    description: str = ""
    input: dict[str, FieldSpec] = Field(default_factory=dict)
    nodes: dict[str, Node]
    edges: list[Edge] = Field(default_factory=list)
    output: str = ""
    """Path of the run's result (e.g. ``classify`` or ``classify.sender``);
    empty ⇒ every node result."""

    @field_validator("nodes")
    @classmethod
    def _non_empty(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("a graph needs at least one node")
        return v

    def result_key(self, node_id: str) -> str:
        node = self.nodes[node_id]
        return node.save_as or node_id

    def to_file(self) -> str:
        """Serialize to the canonical on-disk JSON (name omitted: it's the filename)."""
        data = self.model_dump(by_alias=True, exclude_defaults=True, exclude={"name"})
        data["nodes"] = {
            nid: {"type": n.type, **n.model_dump(by_alias=True, exclude_defaults=True)}
            for nid, n in self.nodes.items()
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def template_roots(value: Any) -> set[str]:
    """Return the root keys every ``{{ path }}`` in *value* (recursively) reads."""
    roots: set[str] = set()
    if isinstance(value, str):
        roots.update(m.group(1).split(".", 1)[0] for m in TEMPLATE_RE.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            roots |= template_roots(v)
    elif isinstance(value, list):
        for v in value:
            roots |= template_roots(v)
    return roots


def parse_graph_spec(
    name: str,
    raw: str | dict[str, Any],
    *,
    available_tools: set[str] | None = None,
    available_subagents: set[str] | None = None,
) -> GraphSpec:
    """Parse and fully validate a graph workflow.

    Args:
        name: The workflow name (the filename stem).
        raw: The file's JSON text, or an already-decoded dict.
        available_tools: When given, every ``tool`` node must name one of these.
        available_subagents: When given, every ``subagent`` node must name one.

    Raises:
        GraphSpecError: listing *every* problem found, so an editor can show them
            all at once instead of one per save.
    """
    errors: list[str] = []
    if not _SAFE_ID.match(name or ""):
        errors.append(
            f"name {name!r}: use snake_case — a letter, then letters, digits or underscores"
        )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GraphSpecError(name, [*errors, f"not valid JSON: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise GraphSpecError(name, [*errors, "the file must be a JSON object"])
    try:
        spec = GraphSpec.model_validate({**raw, "name": name})
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        raise GraphSpecError(name, errors) from exc

    errors.extend(_check_structure(spec, available_tools, available_subagents))
    if errors:
        raise GraphSpecError(name, errors)
    return spec


def _check_structure(
    spec: GraphSpec,
    available_tools: set[str] | None,
    available_subagents: set[str] | None,
) -> list[str]:
    errors: list[str] = []
    ids = set(spec.nodes)
    targets = ids | {END}

    result_keys: dict[str, str] = {}
    for nid, node in spec.nodes.items():
        if not _SAFE_ID.match(nid) or nid in _RESERVED:
            errors.append(f"node {nid!r}: ids must be snake_case and not START/END/input")
        if node.type == "branch":
            continue  # a branch only routes; it stores no result
        key = spec.result_key(nid)
        if node.save_as and (not _SAFE_ID.match(key) or key in _RESERVED):
            errors.append(f"node {nid!r}: save_as {key!r} must be snake_case and not 'input'")
        if key in result_keys:
            errors.append(f"node {nid!r}: save_as {key!r} is already used by {result_keys[key]!r}")
        result_keys.setdefault(key, nid)

    # Edges.
    outgoing: dict[str, list[str]] = {nid: [] for nid in ids}
    start_targets: list[str] = []
    for i, edge in enumerate(spec.edges):
        where = f"edges[{i}]"
        sources = edge.sources
        if not sources:
            errors.append(f"{where}: 'from' is empty")
        for src in sources:
            if src == START:
                if len(sources) > 1:
                    errors.append(f"{where}: START cannot be combined with other sources")
                continue
            if src not in ids:
                errors.append(f"{where}: unknown source node {src!r}")
            elif spec.nodes[src].type == "branch":
                errors.append(
                    f"{where}: branch node {src!r} routes by its rules — remove this edge"
                )
            else:
                outgoing[src].append(edge.to)
        if edge.to not in targets:
            errors.append(f"{where}: unknown target {edge.to!r}")
        if sources == [START]:
            start_targets.append(edge.to)
    if not start_targets:
        errors.append("no edge from START — add {'from': 'START', 'to': '<first node>'}")

    # Node-specific references.
    readable = {"input", *result_keys}
    for nid, node in spec.nodes.items():
        if node.type == "branch":
            for j, rule in enumerate(node.rules):
                if rule.then not in targets:
                    errors.append(f"node {nid!r}: rules[{j}].then {rule.then!r} is not a node")
                root = rule.if_.path.split(".", 1)[0]
                if root not in readable:
                    errors.append(f"node {nid!r}: rules[{j}] reads unknown key {root!r}")
            if node.else_ not in targets:
                errors.append(f"node {nid!r}: else {node.else_!r} is not a node")
        if node.type == "human_review":
            if node.on_reject not in targets:
                errors.append(f"node {nid!r}: on_reject {node.on_reject!r} is not a node")
            for key in node.show:
                if key.split(".", 1)[0] not in readable:
                    errors.append(f"node {nid!r}: show lists unknown key {key!r}")
            if node.editable and node.editable not in readable - {"input"}:
                errors.append(f"node {nid!r}: editable {node.editable!r} is not a node result")
        if node.type == "tool" and available_tools is not None and node.tool not in available_tools:
            errors.append(f"node {nid!r}: tool {node.tool!r} is not available")
        if (
            node.type == "subagent"
            and available_subagents is not None
            and node.subagent not in available_subagents
        ):
            errors.append(f"node {nid!r}: subagent {node.subagent!r} is not registered")
        fields = node.model_dump(by_alias=True)
        for root in sorted(template_roots(fields) - readable):
            errors.append(f"node {nid!r}: template reads unknown key {root!r}")

    if spec.output and spec.output.split(".", 1)[0] not in readable:
        errors.append(f"output {spec.output!r} reads an unknown key")

    # Reachability from START.
    seen: set[str] = set()
    frontier = [t for t in start_targets if t in ids]
    while frontier:
        nid = frontier.pop()
        if nid in seen:
            continue
        seen.add(nid)
        node = spec.nodes[nid]
        nxt = list(outgoing.get(nid, []))
        if node.type == "branch":
            nxt += [r.then for r in node.rules] + [node.else_]
        if node.type == "human_review":
            nxt.append(node.on_reject)
        frontier.extend(t for t in nxt if t in ids)
    for nid in spec.nodes:
        if nid not in seen:
            errors.append(f"node {nid!r} is unreachable from START")
    return errors


#: Suffix of graph workflow files in the workflows folder.
GRAPH_SUFFIX = ".graph.json"


def load_graph_files(
    directory: Path | str,
) -> tuple[dict[str, GraphSpec], dict[str, GraphSpecError]]:
    """Parse every ``<name>.graph.json`` in *directory*.

    Returns ``(valid, invalid)``: valid specs by name, and the error for each file
    that failed validation (so a UI can show *why* a workflow didn't load).
    Tool/subagent availability is not checked here — that happens at run start.
    """
    directory = Path(directory)
    valid: dict[str, GraphSpec] = {}
    invalid: dict[str, GraphSpecError] = {}
    if not directory.is_dir():
        return valid, invalid
    for path in sorted(directory.glob(f"*{GRAPH_SUFFIX}")):
        name = path.name[: -len(GRAPH_SUFFIX)]
        try:
            valid[name] = parse_graph_spec(name, path.read_text(encoding="utf-8"))
        except GraphSpecError as exc:
            invalid[name] = exc
    return valid, invalid
