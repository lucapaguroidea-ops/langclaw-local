"""
Langclaw workflows — named, checkpointed LangGraph procedures.

A workflow is a LangGraph ``StateGraph``, written in Python
(``app.workflow("name", graph=builder)``) or as a ``workflows/<name>.graph.json``
file. Runs are checkpointed on the gateway's checkpointer (crash-resumable), can
pause for human review, and are exposed as ``workflow_<name>`` tools, the
``/workflows`` command, cron jobs, and the control-plane API.

- :mod:`registry` — ``WorkflowSpec`` + ``WorkflowRegistry`` (names, collisions).
- :mod:`graph`    — the file format, compiler, runner, run index, and node helpers
  (``steps()``, ``request_review()``).
- :mod:`runtime`  — ``WorkflowRuntime``: the entry point every surface calls.
- :mod:`executor` — ``StepRequest`` / ``build_toolset_executor``: how nodes reach
  the live tools, model, and subagents.
- :mod:`bridge`   — ``workflow_<name>`` agent tools and the prompt nudge.
- :mod:`progress` — per-node progress lines projected to the invoking channel.
- :mod:`store`    — the ``BaseStore`` backend (SQLite/Postgres) for run records.
"""

from __future__ import annotations

from langclaw.workflows.bridge import (
    WORKFLOW_TOOL_PREFIX,
    make_workflow_tools,
    resolve_workflow_ptc_names,
    workflow_system_prompt,
)
from langclaw.workflows.executor import (
    StepExecutor,
    StepRequest,
    WorkflowStepError,
    build_toolset_executor,
)
from langclaw.workflows.graph import (
    GraphRunResult,
    GraphSpec,
    GraphSpecError,
    GraphWorkflowRunner,
    ReviewAlreadyResolved,
    RunIndex,
    request_review,
    steps,
)
from langclaw.workflows.progress import (
    emit_progress,
    render_workflow_progress,
    reset_progress_sink,
    set_progress_sink,
)
from langclaw.workflows.registry import WorkflowRegistry, WorkflowSpec
from langclaw.workflows.runtime import WorkflowRuntime
from langclaw.workflows.store import (
    MemoryWorkflowStoreBackend,
    WorkflowStoreBackend,
    make_workflow_store_backend,
)

__all__ = [
    "WORKFLOW_TOOL_PREFIX",
    "GraphRunResult",
    "GraphSpec",
    "GraphSpecError",
    "GraphWorkflowRunner",
    "MemoryWorkflowStoreBackend",
    "ReviewAlreadyResolved",
    "RunIndex",
    "StepExecutor",
    "StepRequest",
    "WorkflowRegistry",
    "WorkflowRuntime",
    "WorkflowSpec",
    "WorkflowStepError",
    "WorkflowStoreBackend",
    "build_toolset_executor",
    "emit_progress",
    "make_workflow_tools",
    "make_workflow_store_backend",
    "render_workflow_progress",
    "request_review",
    "reset_progress_sink",
    "resolve_workflow_ptc_names",
    "set_progress_sink",
    "steps",
    "workflow_system_prompt",
]
