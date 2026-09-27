"""
Step helpers for graph workflow nodes — langclaw tools, models, subagents, reviews.

A graph node is plain LangGraph code; these helpers are how it reaches langclaw's
live capabilities without importing the app. The runner installs the current
run's :class:`WorkflowSteps` in a context variable for the duration of each
``ainvoke``, so a Python-authored graph reads it the same way a file-authored
one does::

    from langclaw.workflows.graph import request_review, steps

    async def classify(state):
        meta = await steps().llm(f"Who sent this?\\n{state['text']}", schema=Meta)
        return {"meta": meta.model_dump()}

    async def review(state):
        decision = request_review("Is this right?", data={"meta": state["meta"]})
        if decision["action"] == "reject":
            return {"rejected": True}
        return {"meta": decision.get("data", {}).get("meta", state["meta"])}
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any

from langclaw.workflows.executor import StepExecutor, StepRequest, WorkflowStepError

_CURRENT: contextvars.ContextVar[WorkflowSteps | None] = contextvars.ContextVar(
    "langclaw_workflow_steps", default=None
)

#: Decisions a reviewer can make on a ``human_review`` pause.
REVIEW_ACTIONS = ("approve", "edit", "reject")


class WorkflowSteps:
    """langclaw capabilities available to the nodes of one workflow run."""

    def __init__(
        self,
        executor: StepExecutor,
        *,
        run_id: str = "",
        role: str = "",
        allowed_tool: Callable[[str], bool] | None = None,
    ) -> None:
        self._executor = executor
        self.run_id = run_id
        #: RBAC role of whoever started the run ("" when permissions are off).
        self.role = role
        self._allowed_tool = allowed_tool

    async def llm(
        self,
        prompt: str,
        *,
        schema: type | None = None,
        system: str = "",
        model: str = "",
    ) -> Any:
        """One model call. Returns text, or a validated *schema* instance."""
        request = StepRequest(
            kind="llm",
            target=model,
            payload={"prompt": prompt, "system": system},
            schema=schema,
        )
        result = await self._executor(request)
        if schema is not None and isinstance(result, dict):
            return schema(**result)
        return result

    async def tool(self, name: str, **kwargs: Any) -> Any:
        """Call a registered tool by name.

        langclaw tools report failure by *returning* ``{"error": ...}`` (or an
        ``"Error: ..."`` string) rather than raising, so the agent can recover.
        A workflow has no one to read that, so it becomes a failed step here —
        the run stops and shows the error instead of "completing" with nothing
        done. Catch :class:`WorkflowStepError` in a code node to handle it.

        With RBAC on, the tool must be granted to the role of whoever started
        the run (``permissions.roles.<role>.tools``) — the same rule the agent
        follows.

        Raises:
            WorkflowStepError: the role may not use the tool, the tool is
                missing, or it returned an error.
        """
        if self._allowed_tool is not None and not self._allowed_tool(name):
            raise WorkflowStepError(
                f"The run's role {self.role!r} may not use tool {name!r} — grant it in "
                f"permissions.roles.{self.role}.tools."
            )
        result = await self._executor(StepRequest(kind="tool", target=name, payload=kwargs))
        error = tool_error(result)
        if error:
            raise WorkflowStepError(f"Tool {name!r} failed: {error}")
        return result

    async def subagent(self, subagent_type: str, prompt: str) -> str:
        """Delegate to a registered subagent and return its final reply."""
        return await self._executor(
            StepRequest(kind="subagent", target=subagent_type, payload=prompt)
        )


def tool_error(result: Any) -> str:
    """The error a tool *returned*, or ``""``: ``{"error": "..."}`` or ``"Error: ..."``."""
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    if isinstance(result, str) and result.startswith("Error:"):
        return result.removeprefix("Error:").strip()
    return ""


def steps() -> WorkflowSteps:
    """Return the current run's :class:`WorkflowSteps`.

    Raises:
        WorkflowStepError: when called outside a langclaw workflow run.
    """
    current = _CURRENT.get()
    if current is None:
        raise WorkflowStepError(
            "steps() is only available inside a langclaw workflow run "
            "(started via /workflows run, the workflow_<name> tool, cron, or the API)."
        )
    return current


def set_steps(value: WorkflowSteps | None) -> contextvars.Token:
    """Install *value* as the current run's steps (runner-internal)."""
    return _CURRENT.set(value)


def reset_steps(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def request_review(
    message: str,
    *,
    data: dict[str, Any] | None = None,
    editable: str = "",
    node: str = "",
) -> dict[str, Any]:
    """Pause the run until a person decides, then return their decision.

    Wraps LangGraph's ``interrupt`` with langclaw's review payload, so the pause
    shows up in ``/workflows reviews``, the API, and the UI like a file-authored
    ``human_review`` node. The node re-executes from the top when resumed (a
    LangGraph rule), so keep side effects *after* this call.

    Returns:
        ``{"action": "approve" | "edit" | "reject", "data": {...}, "comment": str,
        "by": str, "via": str}``. On ``edit``, ``data`` holds the corrected value
        under the *editable* key.
    """
    from langgraph.types import interrupt

    decision = interrupt(
        {
            "kind": "review",
            "node": node,
            "message": message,
            "data": data or {},
            "editable": editable,
        }
    )
    return normalize_decision(decision)


def normalize_decision(raw: Any) -> dict[str, Any]:
    """Coerce a resume value to the decision shape; unknown actions are rejected."""
    if isinstance(raw, str):
        raw = {"action": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"A review decision must be an object, got {type(raw).__name__}.")
    action = str(raw.get("action", "")).lower()
    if action not in REVIEW_ACTIONS:
        raise ValueError(
            f"Unknown review action {action!r}; use one of {', '.join(REVIEW_ACTIONS)}."
        )
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("A review decision's 'data' must be an object.")
    return {
        "action": action,
        "data": data,
        "comment": str(raw.get("comment", "")),
        "by": str(raw.get("by", "")),
        "via": str(raw.get("via", "")),
    }
