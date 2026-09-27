"""
Workflow registry — the named, collision-checked set of LangGraph workflows.

A :class:`WorkflowRegistry` stores :class:`WorkflowSpec` entries and refuses a
name that collides with an existing workflow or any reserved name (tools,
subagents, named agents, commands), so dispatch is never ambiguous.

Every workflow is a LangGraph ``StateGraph``, from one of two sources:

- **code** — a developer's graph, ``app.workflow("name", graph=builder)``;
- **file** — ``workflows/<name>.graph.json``, parsed into a
  :class:`~langclaw.workflows.graph.spec.GraphSpec` and compiled to a graph
  (editable from the UI or by the agent).

This module is pure and import-light so it can be unit-tested in isolation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class WorkflowSpec:
    """A registered workflow.

    Attributes:
        name:            Unique handle (tool ``workflow_<name>``, ``/workflows run``,
                         cron, API).
        graph:           The uncompiled LangGraph ``StateGraph``; the runner
                         compiles it with the gateway checkpointer.
        description:     What the workflow does — the tool description the LLM reads.
        input_model:     Optional Pydantic model validating the run input.
        output_model:    Optional Pydantic model describing the output (informational).
        max_steps:       LangGraph ``recursion_limit`` per run (``None`` → global).
        max_concurrency: Max nodes running in parallel within one run.
        timeout_s:       Per-run wall-clock budget in seconds (``None`` → none).
        uses_tools:      Tool names this workflow declares it needs.
        output_key:      For a Python graph, the state key returned as the run's
                         output (``""`` ⇒ the whole final state).
        graph_spec:      The parsed file for a file-authored workflow; ``None`` for
                         a graph written in Python.
    """

    name: str
    graph: Any
    description: str = ""
    input_model: type | None = None
    output_model: type | None = None
    max_steps: int | None = None
    max_concurrency: int = 8
    timeout_s: float | None = None
    uses_tools: list[str] = field(default_factory=list)
    output_key: str = ""
    graph_spec: Any = None

    def __post_init__(self) -> None:
        if self.graph is None or not hasattr(self.graph, "compile"):
            raise ValueError(
                f"Workflow {self.name!r}: `graph` must be an uncompiled LangGraph "
                "StateGraph (langclaw compiles it with the gateway checkpointer)."
            )

    @property
    def source(self) -> str:
        """``"file"`` for a ``.graph.json`` workflow, ``"code"`` for a Python graph."""
        return "file" if self.graph_spec is not None else "code"

    def validate_input(self, value: Any) -> Any:
        """Coerce/validate *value* against ``input_model`` when declared.

        Returns the validated model instance (or the raw value when no model is
        declared). Raises whatever the Pydantic model raises on bad input.
        """
        if self.input_model is None:
            return value
        if isinstance(value, self.input_model):
            return value
        # Models often pass the input as a JSON *string*; decode it first.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass  # fall through to model_validate for a clean, typed error
        if isinstance(value, dict):
            return self.input_model(**value)
        return self.input_model.model_validate(value)


class WorkflowRegistry:
    """An ordered, collision-checked store of :class:`WorkflowSpec`."""

    def __init__(self) -> None:
        self._by_name: dict[str, WorkflowSpec] = {}
        # Bumped on every (de)registration. The gateway compares it to rebuild the
        # agent when a workflow file is added/edited/removed, so the matching
        # `workflow_<name>` tool goes live in the same session.
        self._version = 0

    @property
    def version(self) -> int:
        """Monotonic registry version — changes whenever a workflow is (de)registered."""
        return self._version

    def register(
        self,
        spec: WorkflowSpec,
        *,
        reserved_names: Iterable[str] = (),
    ) -> WorkflowSpec:
        """Register *spec*, rejecting name collisions.

        Raises:
            ValueError: If the name is empty, already registered, or reserved.
        """
        name = spec.name
        if not name or not name.strip():
            raise ValueError("Workflow name must be a non-empty string.")
        if name in self._by_name:
            raise ValueError(f"Workflow {name!r} is already registered.")
        if name in set(reserved_names):
            raise ValueError(
                f"Workflow name {name!r} collides with an existing tool, "
                "subagent, agent, or command. Choose a unique name."
            )
        self._by_name[name] = spec
        self._version += 1
        return spec

    def unregister(self, name: str) -> bool:
        """Remove the workflow registered under *name*; return whether one existed."""
        if name in self._by_name:
            del self._by_name[name]
            self._version += 1
            return True
        return False

    def get(self, name: str) -> WorkflowSpec | None:
        """Return the spec registered under *name*, or ``None``."""
        return self._by_name.get(name)

    def names(self) -> list[str]:
        """Registered workflow names, in registration order."""
        return list(self._by_name)

    def specs(self) -> list[WorkflowSpec]:
        """All registered specs, in registration order."""
        return list(self._by_name.values())

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)
