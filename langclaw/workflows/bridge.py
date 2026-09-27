"""
Bridge — expose registered workflows to the agent as ``workflow_<name>`` tools.

- :func:`make_workflow_tools` — one LangChain tool per registered workflow. It
  starts a run through :class:`~langclaw.workflows.runtime.WorkflowRuntime` and
  returns the output, or — when the run pauses for human review — what it is
  waiting on and how to answer. Failures come back as ``"Error: ..."`` strings
  (the cron-tool convention) instead of raising into the agent loop.
- :func:`workflow_system_prompt` — the ``<workflows>`` nudge listing them.
- :func:`resolve_workflow_ptc_names` — which workflow tools the ``eval``
  interpreter may call.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, Field

from langclaw.naming import WORKFLOW_TOOL_PREFIX, workflow_tool_name

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from langclaw.config.schema import PermissionsConfig, WorkflowsConfig
    from langclaw.workflows.registry import WorkflowRegistry, WorkflowSpec
    from langclaw.workflows.runtime import WorkflowRuntime

# The ``workflow_<name>`` tool/PTC prefix is owned by ``langclaw.naming``;
# re-exported here for back-compat with existing imports.
__all__ = [
    "WORKFLOW_TOOL_PREFIX",
    "make_workflow_tools",
    "resolve_workflow_ptc_names",
    "workflow_system_prompt",
]


def resolve_workflow_ptc_names(
    registry: WorkflowRegistry,
    *,
    workflows_config: WorkflowsConfig,
    permissions_config: PermissionsConfig | None = None,
    role: str | None = None,
) -> list[str]:
    """Resolve which ``workflow_<name>`` tools a script may call (Mode 1).

    Pure and side-effect free — the workflow-axis analogue of
    :func:`langclaw.interpreter.resolve_ptc_allowlist`.  Resolution:

    1. Workflows disabled → ``[]``.
    2. Otherwise every registered workflow, as ``workflow_<name>``.
    3. When ``role`` + an enabled ``permissions_config`` are given, narrow to the
       role's :func:`~langclaw.middleware.permissions.allowed_workflow_names`
       (**default-deny**) — so a script can never reach a workflow the role lacks
       and the PTC surface cannot drift from the live ``workflow_<name>`` tool
       gate (both resolve through :func:`langclaw.rbac.resolve_capability`).

    Returns:
        Sorted ``workflow_<name>`` tool names to merge into the interpreter's
        PTC allowlist.
    """
    if not workflows_config.enabled:
        return []

    names = registry.names()
    if role is not None and permissions_config is not None and permissions_config.enabled:
        from langclaw.middleware.permissions import allowed_workflow_names

        permitted = allowed_workflow_names(permissions_config, role, names)
        names = [n for n in names if n in permitted]

    return sorted(f"{WORKFLOW_TOOL_PREFIX}{n}" for n in names)


class _WorkflowToolArgs(BaseModel):
    """Argument schema shared by every ``workflow_<name>`` tool.

    Defined once at module scope (not per tool build) so repeated agent
    construction does not register many same-named pydantic models.
    """

    workflow_input: Any = Field(
        default=None,
        description="Input object for the workflow (validated against its schema).",
    )


def make_workflow_tools(registry: WorkflowRegistry, runtime: WorkflowRuntime) -> list[BaseTool]:
    """Build one ``workflow_<name>`` LangChain tool per registered workflow.

    Each tool takes a single ``workflow_input`` argument (validated against the
    workflow's input model), starts a run, and returns its output — or, when the
    run pauses for review, a message saying so and how to answer.
    """
    from langchain_core.tools import StructuredTool

    return [_make_one_workflow_tool(spec, runtime, StructuredTool) for spec in registry.specs()]


def _make_one_workflow_tool(
    spec: WorkflowSpec, runtime: WorkflowRuntime, structured_tool_cls: type[BaseTool]
) -> BaseTool:
    description = spec.description or f"Run the {spec.name!r} workflow."

    async def _run(workflow_input: Any = None) -> str:
        run_id = f"{spec.name}:{uuid.uuid4().hex[:12]}"
        try:
            result = await runtime.run_graph(spec, workflow_input, run_id=run_id, trigger="agent")
            return result.to_text()
        except Exception as exc:  # noqa: BLE001 — surfaced to the agent as text
            logger.warning(f"Workflow {spec.name!r} run {run_id} failed: {exc}")
            return f"Error: workflow {spec.name!r} failed: {exc}"

    return structured_tool_cls.from_function(
        coroutine=_run,
        name=workflow_tool_name(spec.name),
        description=description,
        args_schema=_WorkflowToolArgs,
    )


def workflow_system_prompt(registry: WorkflowRegistry) -> str:
    """The ``<workflows>`` system-prompt block listing registered workflows.

    Without it the ``workflow_<name>`` tools are present but unexplained, so the
    model rarely reaches for them. Returns ``""`` when none are registered.
    """
    specs = registry.specs()
    if not specs:
        return ""
    lines = []
    for s in specs:
        desc = f" — {s.description}" if s.description else ""
        lines.append(f"  - {workflow_tool_name(s.name)}{desc}")
    listing = "\n".join(lines)
    return (
        "<workflows>\n"
        "Workflows are saved, multi-step LangGraph procedures exposed as "
        "`workflow_<name>` tools. When a request matches one, run that tool instead "
        "of improvising the same steps yourself. A run may pause for human review; "
        "if the tool says so, tell the user how to answer (the message includes the "
        "commands) rather than retrying.\n"
        f"Available workflows:\n{listing}\n"
        "</workflows>"
    )
