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

from langchain.tools import ToolRuntime
from loguru import logger
from pydantic import BaseModel, Field

from langclaw.naming import WORKFLOW_TOOL_PREFIX, workflow_tool_name

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from langclaw.config.schema import PermissionsConfig, WorkflowsConfig
    from langclaw.workflows.files import WorkflowFiles
    from langclaw.workflows.registry import WorkflowRegistry, WorkflowSpec
    from langclaw.workflows.runtime import WorkflowRuntime

# The ``workflow_<name>`` tool/PTC prefix is owned by ``langclaw.naming``;
# re-exported here for back-compat with existing imports.
__all__ = [
    "WORKFLOW_TOOL_PREFIX",
    "make_manage_workflows_tool",
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


def _origin(tool_runtime: Any) -> dict[str, str] | None:
    """The chat a tool call came from (so a review request goes back there)."""
    ctx = getattr(tool_runtime, "context", None)
    channel = getattr(ctx, "channel", "") if ctx is not None else ""
    if not channel:
        return None
    return {
        "channel": channel,
        "user_id": getattr(ctx, "user_id", ""),
        "context_id": getattr(ctx, "context_id", ""),
        "chat_id": getattr(ctx, "chat_id", ""),
    }


def _make_one_workflow_tool(
    spec: WorkflowSpec, wf_runtime: WorkflowRuntime, structured_tool_cls: type[BaseTool]
) -> BaseTool:
    description = spec.description or f"Run the {spec.name!r} workflow."

    async def _run(workflow_input: Any = None, runtime: ToolRuntime = None) -> str:  # type: ignore[assignment]
        run_id = f"{spec.name}:{uuid.uuid4().hex[:12]}"
        try:
            result = await wf_runtime.run_graph(
                spec,
                workflow_input,
                run_id=run_id,
                trigger="agent",
                reply_to=_origin(runtime),
            )
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


#: Compact format reference shown to the model (the full guide is docs/guides/workflows.md).
GRAPH_FORMAT_HELP = """\
A workflow file is JSON: {"description": str, "input": {field: {"type", "description"}},
"nodes": {id: node}, "edges": [{"from": "START"|id|[ids], "to": id|"END"}], "output": path}.
Node types:
- {"type": "llm", "prompt": str, "system"?: str, "output"?: {field: {"type": "string"|"number"|
  "integer"|"boolean"|"array"|"object", "description"?}}}  one model call; "output" = structured
- {"type": "tool", "tool": name, "args": {...}}  call one tool
- {"type": "subagent", "subagent": name, "prompt": str}
- {"type": "branch", "rules": [{"if": {"path", "op", "value"}, "then": id}], "else": id|"END"}
  ops: eq ne lt le gt ge in not_in contains exists not_exists truthy falsy.
  No edges out of a branch.
- {"type": "human_review", "message": str, "show"?: [keys], "editable"?: key,
  "on_reject"?: id|"END"}  pauses the run until the user approves, edits, or rejects.
Each node's result is stored under its id; templates like {{input.x}} or {{node_id.field}} read
them. Node ids are snake_case; a node with no outgoing edge ends the run."""


class _ManageWorkflowsArgs(BaseModel):
    action: str = Field(
        description="One of: list, get, validate, save, delete, versions, restore, format."
    )
    name: str = Field(default="", description="Workflow name (snake_case).")
    graph: Any = Field(
        default=None,
        description="For validate/save: the workflow file (a JSON object or JSON string).",
    )
    version: str = Field(default="", description="For restore: a version id from `versions`.")


def make_manage_workflows_tool(files: WorkflowFiles) -> BaseTool:
    """The ``manage_workflows`` tool: list, read, validate, save, delete, and restore
    ``workflows/<name>.graph.json`` files through :class:`WorkflowFiles` (the same
    validated, versioned path the UI uses). Errors come back as ``{"error": ...}``."""
    from langchain_core.tools import StructuredTool

    from langclaw.workflows.files import WorkflowFileNotFound

    def _run(action: str, name: str = "", graph: Any = None, version: str = "") -> dict:
        action = (action or "").strip().lower()
        try:
            if action == "format":
                return {"format": GRAPH_FORMAT_HELP}
            if action == "list":
                return {"workflows": files.names()}
            if not name:
                return {"error": f"'{action}' needs a workflow name."}
            if action == "get":
                return {"name": name, "graph": files.read(name)}
            if action == "versions":
                return {"name": name, "versions": files.versions(name)}
            if action == "restore":
                if not version:
                    return {"error": "restore needs a version (see action='versions')."}
                return files.restore(name, version)
            if action == "delete":
                files.delete(name)
                return {"deleted": name, "note": "Its history is kept; restore can bring it back."}
            if action in ("validate", "save"):
                if graph is None:
                    return {"error": f"'{action}' needs the workflow file in 'graph'."}
                if action == "validate":
                    return files.validate(name, graph)
                check = files.validate(name, graph)
                if not check["valid"]:
                    return {"error": "The workflow is invalid.", "errors": check["errors"]}
                return files.save(name, graph)
            return {"error": f"Unknown action {action!r}."}
        except WorkflowFileNotFound as exc:
            return {"error": str(exc)}
        except ValueError as exc:
            return {"error": str(exc)}

    return StructuredTool.from_function(
        func=_run,
        name="manage_workflows",
        description=(
            "Create, edit, inspect, or delete saved workflows (workflows/<name>.graph.json). "
            "Actions: list; get(name); validate(name, graph); save(name, graph) — validates, "
            "keeps the previous version, and makes it live as workflow_<name>; delete(name); "
            "versions(name); restore(name, version); format — the file format reference. "
            "Always validate or call format first when unsure, and confirm with the user "
            "before delete.\n\n" + GRAPH_FORMAT_HELP
        ),
        args_schema=_ManageWorkflowsArgs,
    )


def workflow_system_prompt(registry: WorkflowRegistry, *, authoring: bool = False) -> str:
    """The ``<workflows>`` system-prompt block listing registered workflows.

    Without it the ``workflow_<name>`` tools are present but unexplained, so the
    model rarely reaches for them. With *authoring*, it also says the agent can
    create and edit workflows with ``manage_workflows``. Returns ``""`` when there
    is nothing to say.
    """
    specs = registry.specs()
    if not specs and not authoring:
        return ""
    lines = []
    for s in specs:
        desc = f" — {s.description}" if s.description else ""
        lines.append(f"  - {workflow_tool_name(s.name)}{desc}")
    listing = "\n".join(lines) or "  (none yet)"
    authoring_line = (
        "You can create or change workflows with `manage_workflows` when the user asks "
        "for a repeatable procedure (validate first; confirm before deleting). A saved "
        "workflow becomes a `workflow_<name>` tool on your next turn.\n"
        if authoring
        else ""
    )
    return (
        "<workflows>\n"
        "Workflows are saved, multi-step LangGraph procedures exposed as "
        "`workflow_<name>` tools. When a request matches one, run that tool instead "
        "of improvising the same steps yourself. A run may pause for human review; "
        "if the tool says so, tell the user how to answer (the message includes the "
        "commands) rather than retrying.\n"
        f"{authoring_line}"
        f"Available workflows:\n{listing}\n"
        "</workflows>"
    )
