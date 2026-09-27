"""
Turn a :class:`~langclaw.workflows.graph.spec.GraphSpec` into a LangGraph ``StateGraph``.

The produced graph is ordinary LangGraph: the runner compiles it with the
gateway's checkpointer, so every node boundary is a checkpoint, a crash resumes
from the last finished node, and a ``human_review`` node pauses via
``interrupt()``. Nodes reach langclaw's tools / models / subagents through
:func:`~langclaw.workflows.graph.steps.steps` (installed per run by the runner).

State::

    {"input": <run input>, "data": {<save_as key>: <node result>, ...}}

``data`` merges dict updates, so parallel branches can write side by side.
"""

from __future__ import annotations

import json
import operator
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from pydantic import create_model

from langclaw.workflows.graph.spec import END, START, TEMPLATE_RE, FieldSpec, GraphSpec
from langclaw.workflows.graph.steps import normalize_decision, steps

if TYPE_CHECKING:
    from langgraph.graph import StateGraph

_PY_TYPES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _merge(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class GraphState(TypedDict, total=False):
    """State of a file-authored graph workflow."""

    input: Any
    data: Annotated[dict[str, Any], _merge]


# -- templating ----------------------------------------------------------------

_MISSING = object()


def resolve_path(namespace: dict[str, Any], path: str) -> Any:
    """Read ``a.b.0.c`` from *namespace*; a missing segment yields ``None``."""
    value: Any = namespace
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            value = getattr(value, part, _MISSING)
        if value is _MISSING:
            return None
    return value


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def render(value: Any, namespace: dict[str, Any]) -> Any:
    """Fill ``{{ path }}`` placeholders in *value* (recursively).

    A string that is exactly one placeholder becomes the raw value (so a tool
    argument can receive a dict or list); placeholders inside longer text are
    rendered as text (JSON for structured values).
    """
    if isinstance(value, str):
        whole = TEMPLATE_RE.fullmatch(value.strip())
        if whole:
            return resolve_path(namespace, whole.group(1))
        return TEMPLATE_RE.sub(lambda m: _as_text(resolve_path(namespace, m.group(1))), value)
    if isinstance(value, dict):
        return {k: render(v, namespace) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, namespace) for v in value]
    return value


def namespace_of(state: GraphState) -> dict[str, Any]:
    return {**(state.get("data") or {}), "input": state.get("input")}


# -- branch conditions ---------------------------------------------------------

_COMPARE = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}


def evaluate(op: str, actual: Any, expected: Any) -> bool:
    """Evaluate one branch condition; type mismatches are simply ``False``."""
    try:
        if op in _COMPARE:
            if actual is None and op not in ("eq", "ne"):
                return False
            return bool(_COMPARE[op](actual, expected))
        if op == "in":
            return actual in (expected or [])
        if op == "not_in":
            return actual not in (expected or [])
        if op == "contains":
            return expected in (actual or [])
        if op == "exists":
            return actual is not None
        if op == "not_exists":
            return actual is None
        if op == "truthy":
            return bool(actual)
        if op == "falsy":
            return not actual
    except TypeError:
        return False
    raise ValueError(f"Unknown branch operator {op!r}")


# -- results -------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Make a node result checkpoint- and API-friendly."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    content = getattr(value, "content", _MISSING)  # ToolMessage / AIMessage
    if content is not _MISSING and not isinstance(value, dict):
        value = content
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def output_model(node_id: str, fields: dict[str, FieldSpec]) -> type:
    """Build the Pydantic model an ``llm`` node's structured output validates against."""
    definitions: dict[str, Any] = {}
    for name, field in fields.items():
        py_type = _PY_TYPES[field.type]
        if field.required:
            definitions[name] = (py_type, ...)
        else:
            definitions[name] = (py_type | None, None)
    model = create_model(f"{node_id.title().replace('_', '')}Output", **definitions)
    for name, field in fields.items():
        if field.description:
            model.model_fields[name].description = field.description
    model.model_rebuild(force=True)
    return model


# -- nodes ---------------------------------------------------------------------


def _llm_node(node_id: str, node: Any, key: str):
    schema = output_model(node_id, node.output) if node.output else None

    async def run(state: GraphState) -> dict:
        ns = namespace_of(state)
        result = await steps().llm(
            render(node.prompt, ns),
            schema=schema,
            system=render(node.system, ns),
            model=node.model,
        )
        return {"data": {key: to_jsonable(result)}}

    return run


def _tool_node(node: Any, key: str):
    async def run(state: GraphState) -> dict:
        args = render(node.args, namespace_of(state))
        result = await steps().tool(node.tool, **args)
        return {"data": {key: to_jsonable(result)}}

    return run


def _subagent_node(node: Any, key: str):
    async def run(state: GraphState) -> dict:
        reply = await steps().subagent(node.subagent, render(node.prompt, namespace_of(state)))
        return {"data": {key: to_jsonable(reply)}}

    return run


def _review_node(node_id: str, node: Any, key: str):
    from langgraph.types import interrupt

    async def run(state: GraphState) -> dict:
        ns = namespace_of(state)
        shown = node.show or sorted(state.get("data") or {})
        decision = normalize_decision(
            interrupt(
                {
                    "kind": "review",
                    "node": node_id,
                    "message": render(node.message, ns),
                    "data": {k: resolve_path(ns, k) for k in shown},
                    "editable": node.editable,
                }
            )
        )
        update: dict[str, Any] = {key: decision}
        if decision["action"] == "edit" and node.editable:
            edited = decision["data"].get(node.editable, decision["data"])
            current = ns.get(node.editable)
            if isinstance(current, dict) and isinstance(edited, dict):
                edited = {**current, **edited}
            update[node.editable] = edited
        return {"data": update}

    return run


def _branch_node(state: GraphState) -> dict:
    return {}


def _lg(target: str) -> str:
    from langgraph.graph import END as LG_END

    return LG_END if target == END else target


def build_state_graph(spec: GraphSpec) -> StateGraph:
    """Return the (uncompiled) LangGraph ``StateGraph`` for *spec*.

    The spec must already be validated (:func:`parse_graph_spec`).
    """
    from langgraph.graph import START as LG_START
    from langgraph.graph import StateGraph

    graph = StateGraph(GraphState)
    for nid, node in spec.nodes.items():
        key = spec.result_key(nid)
        if node.type == "llm":
            fn = _llm_node(nid, node, key)
        elif node.type == "tool":
            fn = _tool_node(node, key)
        elif node.type == "subagent":
            fn = _subagent_node(node, key)
        elif node.type == "human_review":
            fn = _review_node(nid, node, key)
        else:
            fn = _branch_node
        graph.add_node(nid, fn, metadata={"type": node.type, "label": node.label or nid})

    outgoing: dict[str, list[str]] = {nid: [] for nid in spec.nodes}
    for edge in spec.edges:
        sources = edge.sources
        if sources == [START]:
            graph.add_edge(LG_START, _lg(edge.to))
        elif len(sources) == 1:
            outgoing[sources[0]].append(edge.to)
        else:
            graph.add_edge(sources, _lg(edge.to))  # wait for all sources

    for nid, node in spec.nodes.items():
        targets = outgoing[nid]
        if node.type == "branch":
            graph.add_conditional_edges(nid, _branch_router(node), _path_map(node))
        elif node.type == "human_review":
            graph.add_conditional_edges(
                nid,
                _review_router(spec.result_key(nid), node.on_reject, targets),
                sorted({_lg(t) for t in [*targets, node.on_reject]} | {_lg(END)}),
            )
        elif targets:
            for target in targets:
                graph.add_edge(nid, _lg(target))
        elif not _joins_from(spec, nid):
            graph.add_edge(nid, _lg(END))
    return graph


def _joins_from(spec: GraphSpec, nid: str) -> bool:
    return any(len(e.sources) > 1 and nid in e.sources for e in spec.edges)


def _path_map(node: Any) -> list[str]:
    return sorted({_lg(r.then) for r in node.rules} | {_lg(node.else_)})


def _branch_router(node: Any):
    def route(state: GraphState) -> str:
        ns = namespace_of(state)
        for rule in node.rules:
            cond = rule.if_
            if evaluate(cond.op, resolve_path(ns, cond.path), cond.value):
                return _lg(rule.then)
        return _lg(node.else_)

    return route


def _review_router(key: str, on_reject: str, targets: list[str]):
    def route(state: GraphState) -> list[str] | str:
        decision = (state.get("data") or {}).get(key) or {}
        if decision.get("action") == "reject":
            return _lg(on_reject)
        return [_lg(t) for t in targets] or _lg(END)

    return route
