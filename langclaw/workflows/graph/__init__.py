"""
Graph workflows — LangGraph ``StateGraph`` workflows, checkpointed and reviewable.

Two ways to write one, one engine:

- **In Python** — build a ``StateGraph`` and register it with
  ``app.workflow("name", graph=builder)``. Nodes call langclaw's tools, models and
  subagents via :func:`steps`, and pause for a person via :func:`request_review`.
- **As a file** — ``workflows/<name>.graph.json`` (see
  :mod:`langclaw.workflows.graph.spec`), editable from the UI or by the agent.

Both run on the gateway's checkpointer through :class:`GraphWorkflowRunner`.
"""

from __future__ import annotations

from langclaw.workflows.graph.compile import build_state_graph
from langclaw.workflows.graph.runner import GraphRunResult, GraphWorkflowRunner, thread_id_for
from langclaw.workflows.graph.runs import (
    InMemoryRunIndexBackend,
    ReviewAlreadyResolved,
    RunIndex,
    StoreRunIndexBackend,
)
from langclaw.workflows.graph.spec import (
    GRAPH_SUFFIX,
    GraphSpec,
    GraphSpecError,
    load_graph_files,
    parse_graph_spec,
)
from langclaw.workflows.graph.steps import WorkflowSteps, request_review, steps

__all__ = [
    "GRAPH_SUFFIX",
    "GraphRunResult",
    "GraphSpec",
    "GraphSpecError",
    "GraphWorkflowRunner",
    "InMemoryRunIndexBackend",
    "ReviewAlreadyResolved",
    "RunIndex",
    "StoreRunIndexBackend",
    "WorkflowSteps",
    "build_state_graph",
    "load_graph_files",
    "parse_graph_spec",
    "request_review",
    "steps",
    "thread_id_for",
]
