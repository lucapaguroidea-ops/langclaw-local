"""
``WorkflowRuntime`` — the gateway's single entry point for running workflows.

Owns the global ``max_concurrent_runs`` ceiling and the
:class:`~langclaw.workflows.graph.runner.GraphWorkflowRunner` (checkpointer +
run index), and supplies the runner with the step executor the agent builder
registers (the live toolset, model, and subagents). Every entry point — the
``workflow_<name>`` tool, ``/workflows run``, cron, the API — goes through here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langclaw.workflows.graph.runner import GraphRunResult, GraphWorkflowRunner

if TYPE_CHECKING:
    from langclaw.config.schema import WorkflowsConfig
    from langclaw.workflows.executor import StepExecutor
    from langclaw.workflows.registry import WorkflowSpec

#: ``(tool_runtime | None) -> StepExecutor`` (sync or async), set by the builder.
ExecutorFactory = Callable[[Any], "StepExecutor | Awaitable[StepExecutor]"]


class WorkflowRuntime:
    """Runs workflows under global resource ceilings.

    Args:
        config: The resolved :class:`WorkflowsConfig`.
        runner: The graph runner; ``None`` ⇒ an in-memory one until the app
            attaches the checkpointer-backed runner via :meth:`set_graph_runner`.
    """

    def __init__(
        self, config: WorkflowsConfig, *, runner: GraphWorkflowRunner | None = None
    ) -> None:
        self._config = config
        self._run_gate = asyncio.Semaphore(max(1, config.max_concurrent_runs))
        self._executor_factory: ExecutorFactory | None = None
        self._graph_runner: GraphWorkflowRunner | None = None
        if runner is not None:
            self.set_graph_runner(runner)

    # -- wiring ----------------------------------------------------------------

    def set_executor_factory(self, factory: ExecutorFactory) -> None:
        """Register how nodes reach tools/models/subagents (called by the agent builder)."""
        self._executor_factory = factory

    @property
    def graph_runner(self) -> GraphWorkflowRunner:
        """The graph runner (in-memory until the app sets a durable one)."""
        if self._graph_runner is None:
            self.set_graph_runner(GraphWorkflowRunner(max_steps=self._config.max_steps_per_run))
        return self._graph_runner  # type: ignore[return-value]

    def set_graph_runner(self, runner: GraphWorkflowRunner) -> None:
        """Use *runner* for workflow runs; its nodes use the live agent toolset."""
        runner.set_executor_provider(self._step_executor)
        self._graph_runner = runner

    async def _step_executor(self) -> StepExecutor:
        if self._executor_factory is None:
            raise RuntimeError("Workflow steps need the agent's toolset, which is not built yet.")
        maybe = self._executor_factory(None)
        return await maybe if isinstance(maybe, Awaitable) else maybe

    # -- runs --------------------------------------------------------------------

    async def run_graph(
        self,
        spec: WorkflowSpec,
        run_input: Any,
        *,
        run_id: str,
        trigger: str = "",
        reply_to: dict[str, str] | None = None,
    ) -> GraphRunResult:
        """Start a run; returns when it finishes or pauses for review."""
        validated = spec.validate_input(run_input)
        async with self._run_gate:
            return await self.graph_runner.start(
                spec, validated, run_id=run_id, trigger=trigger, reply_to=reply_to
            )

    #: Alias used by bus dispatch and cron.
    run_registered = run_graph

    async def continue_graph_review(
        self, spec: WorkflowSpec, run_id: str, review: dict[str, Any]
    ) -> GraphRunResult:
        """Continue a run past a review already claimed via the runner."""
        async with self._run_gate:
            return await self.graph_runner.continue_review(spec, run_id, review)

    async def resume_incomplete(self, get_spec: Callable[[str], WorkflowSpec | None]) -> list[str]:
        """Continue runs a previous process left mid-flight (crash recovery)."""
        return await self.graph_runner.resume_incomplete(get_spec)
