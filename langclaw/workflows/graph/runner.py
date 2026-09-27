"""
``GraphWorkflowRunner`` — run, pause, resume, and inspect graph workflows.

Every run is one LangGraph thread (``workflow:<run_id>``) on the gateway's
checkpointer, so:

- a crash resumes from the last finished node (:meth:`resume_incomplete`);
- a ``human_review`` pause survives restarts, and is answered once — the first
  answer (Telegram, UI, or command) wins (:meth:`claim_review`);
- each step's result is inspectable afterwards (:meth:`get_run`).

The :class:`~langclaw.workflows.graph.runs.RunIndex` next to it records which
runs exist, their status, and review answers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from langclaw.workflows.graph.compile import namespace_of, resolve_path, to_jsonable
from langclaw.workflows.graph.runs import RunIndex
from langclaw.workflows.graph.steps import (
    WorkflowSteps,
    normalize_decision,
    reset_steps,
    set_steps,
)
from langclaw.workflows.progress import emit_progress

if TYPE_CHECKING:
    from langclaw.workflows.executor import StepExecutor
    from langclaw.workflows.registry import WorkflowSpec

ExecutorProvider = Callable[[], "StepExecutor | Awaitable[StepExecutor]"]
ReviewHook = Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[None]]


def thread_id_for(run_id: str) -> str:
    """The checkpointer thread a workflow run lives on."""
    return f"workflow:{run_id}"


@dataclass(slots=True)
class GraphRunResult:
    """Where a run stands after it was started or resumed."""

    run_id: str
    workflow: str
    status: str  # "completed" | "waiting" | "rejected"
    output: Any = None
    reviews: list[dict[str, Any]] = field(default_factory=list)

    def to_text(self) -> str:
        """Channel-ready summary: the output, or what the run is waiting on."""
        if self.status == "waiting":
            lines = [f"⏸ Workflow {self.workflow!r} is waiting for review (run {self.run_id})."]
            for review in self.reviews:
                lines.append(f"• {review.get('message', '')}")
            lines.append(
                f"Answer with /workflows approve {self.run_id}, "
                f"/workflows reject {self.run_id}, or "
                f"/workflows edit {self.run_id} <json>."
            )
            return "\n".join(lines)
        if self.status == "rejected":
            return f"Workflow {self.workflow!r} run {self.run_id} was rejected at review."
        return _stringify(self.output)


def _stringify(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class GraphWorkflowRunner:
    """Drives graph workflows on a LangGraph checkpointer.

    Args:
        checkpointer: The gateway's checkpointer (``None`` ⇒ an in-memory saver:
            runs work, but pauses and crash-resume don't survive a restart).
        index: Run records; ``None`` ⇒ in-memory.
        executor_provider: Returns the step executor nodes use for tools, models
            and subagents (set later by the agent builder via
            :meth:`set_executor_provider`).
        max_steps: Default LangGraph ``recursion_limit`` per run.
    """

    def __init__(
        self,
        *,
        checkpointer: Any | None = None,
        index: RunIndex | None = None,
        executor_provider: ExecutorProvider | None = None,
        max_steps: int = 1000,
    ) -> None:
        if checkpointer is None:
            from langgraph.checkpoint.memory import InMemorySaver

            checkpointer = InMemorySaver()
        self._checkpointer = checkpointer
        self.index = index or RunIndex()
        self._executor_provider = executor_provider
        self._max_steps = max_steps
        self._compiled: dict[str, tuple[int, Any]] = {}
        #: Called with ``(run_record, new_reviews)`` when a run pauses for review —
        #: the gateway sends the review requests (e.g. Telegram buttons).
        self.review_hook: ReviewHook | None = None

    def set_executor_provider(self, provider: ExecutorProvider) -> None:
        self._executor_provider = provider

    # -- compilation -----------------------------------------------------------

    def compiled(self, spec: WorkflowSpec) -> Any:
        """The checkpointed LangGraph graph for *spec* (cached per spec object)."""
        cached = self._compiled.get(spec.name)
        if cached is not None and cached[0] == id(spec):
            return cached[1]
        builder = spec.graph
        if builder is None:
            raise ValueError(f"Workflow {spec.name!r} has no graph.")
        compiled = builder.compile(checkpointer=self._checkpointer)
        self._compiled[spec.name] = (id(spec), compiled)
        return compiled

    def mermaid(self, spec: WorkflowSpec) -> str:
        """A Mermaid drawing of the workflow's graph."""
        return self.compiled(spec).get_graph().draw_mermaid()

    # -- lifecycle -------------------------------------------------------------

    async def start(
        self,
        spec: WorkflowSpec,
        run_input: Any,
        *,
        run_id: str,
        trigger: str = "",
        reply_to: dict[str, str] | None = None,
    ) -> GraphRunResult:
        """Start a run and drive it until it finishes or pauses for review."""
        existing = await self.index.get(run_id)
        if existing is not None:
            raise ValueError(f"Run {run_id!r} already exists.")
        await self.index.create(
            run_id, spec.name, to_jsonable(run_input), trigger=trigger, reply_to=reply_to
        )
        if spec.graph_spec is not None:
            first: Any = {"input": to_jsonable(run_input), "data": {}}
        else:
            first = run_input.model_dump() if hasattr(run_input, "model_dump") else run_input
            if first is None:
                first = {}  # no input → start from an empty state (LangGraph needs one)
        logger.info(f"Workflow {spec.name!r} run {run_id} started")
        return await self._drive(spec, run_id, first)

    async def claim_review(
        self,
        run_id: str,
        decision: Any,
        *,
        interrupt_id: str = "",
    ) -> dict[str, Any]:
        """Record the answer to a pending review; the first answer wins.

        Does not continue the run — call :meth:`continue_review` with the result
        (the gateway does so on its bus worker, so the output reaches the run's
        channel).

        Raises:
            KeyError: unknown run.
            ValueError: malformed decision.
            ReviewAlreadyResolved: already answered / nothing pending.
        """
        return await self.index.claim_review(
            run_id, normalize_decision(decision), interrupt_id=interrupt_id
        )

    async def continue_review(
        self, spec: WorkflowSpec, run_id: str, review: dict[str, Any]
    ) -> GraphRunResult:
        """Resume the run past a claimed review."""
        from langgraph.types import Command

        decision = {k: v for k, v in review["decision"].items() if k != "at"}
        logger.info(f"Workflow {spec.name!r} run {run_id} resumed ({decision['action']})")
        return await self._drive(spec, run_id, Command(resume={review["interrupt_id"]: decision}))

    async def resume(
        self, spec: WorkflowSpec, run_id: str, decision: Any, *, interrupt_id: str = ""
    ) -> GraphRunResult:
        """Claim the review and continue the run in one call."""
        review = await self.claim_review(run_id, decision, interrupt_id=interrupt_id)
        return await self.continue_review(spec, run_id, review)

    async def resume_incomplete(self, get_spec: Callable[[str], WorkflowSpec | None]) -> list[str]:
        """Continue every run a previous process left ``running`` (crash recovery).

        Each continues from its last checkpoint — finished nodes do not re-run.
        Runs waiting for review stay paused. Returns the resumed run ids.
        """
        resumed: list[str] = []
        for record in await self.index.list(status="running", limit=10_000):
            run_id = record["run_id"]
            spec = get_spec(record.get("workflow", ""))
            if spec is None or spec.graph is None:
                logger.warning(f"Cannot resume {run_id}: workflow {record.get('workflow')!r} gone.")
                await self.index.update(run_id, status="failed", error="workflow no longer exists")
                continue
            logger.info(f"Resuming workflow run {run_id} ({spec.name!r}) from its checkpoint")
            try:
                await self._drive(spec, run_id, await self._restart_input(spec, record))
            except Exception as exc:  # noqa: BLE001 — one bad run must not block startup
                logger.warning(f"Resumed run {run_id} failed: {exc}")
                continue
            resumed.append(run_id)
        return resumed

    async def _restart_input(self, spec: WorkflowSpec, record: dict[str, Any]) -> Any:
        """What to feed a run on restart: ``None`` (continue), or the answers to
        reviews that were claimed but not yet applied when the process died."""
        from langgraph.types import Command

        snapshot = await self.compiled(spec).aget_state(self._config(spec, record["run_id"]))
        paused = {i.id for i in snapshot.interrupts}
        answers = {
            r["interrupt_id"]: {k: v for k, v in r["decision"].items() if k != "at"}
            for r in record.get("reviews", [])
            if r.get("decision") and r["interrupt_id"] in paused
        }
        return Command(resume=answers) if answers else None

    # -- inspection ------------------------------------------------------------

    async def get_run(self, spec: WorkflowSpec | None, run_id: str) -> dict[str, Any] | None:
        """The run record plus its checkpointed state and step history."""
        record = await self.index.get(run_id)
        if record is None:
            return None
        if spec is None or spec.graph is None:
            return {**record, "state": None, "next": [], "steps": []}
        graph = self.compiled(spec)
        config = self._config(spec, run_id)
        snapshot = await graph.aget_state(config)
        steps: list[dict[str, Any]] = []
        history = [s async for s in graph.aget_state_history(config)]
        for snap in reversed(history):
            for task in snap.tasks:
                if task.name.startswith("__") or (
                    task.result is None and not task.error and not task.interrupts
                ):
                    continue
                steps.append(
                    {
                        "step": (snap.metadata or {}).get("step"),
                        "node": task.name,
                        "result": to_jsonable(task.result),
                        "error": str(task.error) if task.error else "",
                        "paused": bool(task.interrupts),
                        "at": snap.created_at,
                    }
                )
        return {
            **record,
            "state": to_jsonable(snapshot.values),
            "next": list(snapshot.next),
            "steps": steps,
        }

    # -- internals -------------------------------------------------------------

    def _config(self, spec: WorkflowSpec, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id_for(run_id)},
            "recursion_limit": spec.max_steps or self._max_steps,
            "max_concurrency": spec.max_concurrency,
        }

    async def _executor(self) -> StepExecutor:
        if self._executor_provider is None:

            async def _unwired(request: Any) -> Any:
                from langclaw.workflows.executor import WorkflowStepError

                raise WorkflowStepError(
                    "No tools/models are wired for workflow steps yet (the agent "
                    "has not been built)."
                )

            return _unwired
        maybe = self._executor_provider()
        return await maybe if isinstance(maybe, Awaitable) else maybe

    async def _drive(self, spec: WorkflowSpec, run_id: str, graph_input: Any) -> GraphRunResult:
        graph = self.compiled(spec)
        config = self._config(spec, run_id)
        token = set_steps(WorkflowSteps(await self._executor(), run_id=run_id))
        try:
            coro = self._stream(spec, run_id, graph, graph_input, config)
            if spec.timeout_s is not None:
                await asyncio.wait_for(coro, timeout=spec.timeout_s)
            else:
                await coro
        except Exception as exc:
            await self.index.update(run_id, status="failed", error=str(exc) or type(exc).__name__)
            logger.warning(f"Workflow {spec.name!r} run {run_id} failed: {exc}")
            raise
        finally:
            reset_steps(token)
        return await self._settle(spec, run_id, graph, config)

    @staticmethod
    async def _stream(
        spec: WorkflowSpec, run_id: str, graph: Any, graph_input: Any, config: dict[str, Any]
    ) -> None:
        """Run the graph, projecting each finished node to the channel as progress."""
        async for update in graph.astream(graph_input, config, stream_mode="updates"):
            for node in update:
                if node.startswith("__"):
                    continue
                label = node
                if spec.graph_spec is not None and node in spec.graph_spec.nodes:
                    label = spec.graph_spec.nodes[node].label or node
                emit_progress(
                    {"kind": "phase", "workflow": spec.name, "run_id": run_id, "phase": label}
                )

    async def _settle(
        self, spec: WorkflowSpec, run_id: str, graph: Any, config: dict[str, Any]
    ) -> GraphRunResult:
        snapshot = await graph.aget_state(config)
        if snapshot.interrupts:
            pending = [_review_from_interrupt(i) for i in snapshot.interrupts]
            record, added = await self.index.add_reviews(run_id, pending)
            open_reviews = [r for r in record["reviews"] if r.get("decision") is None]
            logger.info(f"Workflow {spec.name!r} run {run_id} waiting for review")
            if added and self.review_hook is not None:
                try:
                    await self.review_hook(record, added)
                except Exception as exc:  # noqa: BLE001 — notifying must not fail the run
                    logger.warning(f"Review notification for {run_id} failed: {exc}")
            return GraphRunResult(run_id, spec.name, "waiting", reviews=open_reviews)

        values = snapshot.values or {}
        if spec.graph_spec is not None:
            ns = namespace_of(values)
            output = (
                resolve_path(ns, spec.graph_spec.output)
                if spec.graph_spec.output
                else values.get("data", {})
            )
        elif spec.output_key:
            output = values.get(spec.output_key)
        else:
            output = {k: v for k, v in values.items() if not k.startswith("__")}
        output = to_jsonable(output)
        record = await self.index.get(run_id) or {}
        answered = [r for r in record.get("reviews", []) if r.get("decision")]
        status = "completed"
        if answered and answered[-1]["decision"].get("action") == "reject":
            status = "rejected"
        await self.index.update(run_id, status=status, output=output, error="")
        logger.info(f"Workflow {spec.name!r} run {run_id} {status}")
        return GraphRunResult(run_id, spec.name, status, output=output)


def _review_from_interrupt(interrupt: Any) -> dict[str, Any]:
    value = interrupt.value
    if not (isinstance(value, dict) and value.get("kind") == "review"):
        value = {"message": _stringify(value), "data": {}, "editable": "", "node": ""}
    return {
        "interrupt_id": interrupt.id,
        "node": value.get("node", ""),
        "message": value.get("message", ""),
        "data": to_jsonable(value.get("data", {})),
        "editable": value.get("editable", ""),
    }
