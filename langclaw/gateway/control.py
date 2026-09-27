"""ControlPlane — the structured management surface of a running gateway.

One implementation of the operations a UI or an operator needs (status,
workflows, workflow runs, schedules), returning plain dicts. Front ends stay
thin: the ``/workflows`` chat command formats these results as text, and the
HTTP :class:`~langclaw.gateway.api.ApiChannel` serialises them as JSON.

Channels receive it the same way they receive the command router — injected by
:class:`~langclaw.gateway.manager.GatewayManager` via
``BaseChannel.set_control_plane()``.

Errors are typed so every front end can map them consistently:

- :class:`NotFoundError` — the named workflow / run / schedule does not exist.
- :class:`FeatureDisabledError` — the operation needs a feature that is off
  (the message says which setting turns it on).
- ``ValueError`` — invalid input (bad name, missing field).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from langclaw.bus.base import InboundMessage

if TYPE_CHECKING:
    import asyncio

    from langclaw.bus.base import BaseMessageBus
    from langclaw.config.schema import LangclawConfig
    from langclaw.cron.scheduler import CronManager
    from langclaw.workflows import WorkflowRegistry
    from langclaw.workflows.saved_store import SavedWorkflowStore


class NotFoundError(LookupError):
    """The requested workflow, run, or schedule does not exist."""


class FeatureDisabledError(RuntimeError):
    """The operation needs a feature that is disabled in config."""


_WORKFLOWS_OFF = "Workflows are disabled. Set LANGCLAW__WORKFLOWS__ENABLED=true."
_SAVED_OFF = (
    "Editing workflows needs saved workflows, which require both "
    "LANGCLAW__WORKFLOWS__ENABLED=true and LANGCLAW__INTERPRETER__ENABLED=true "
    "(plus the 'interpreter' extra) and a filesystem-rooted agent backend."
)
_SCHEDULES_OFF = "Schedules are disabled. Set LANGCLAW__CRON__ENABLED=true."


class ControlPlane:
    """Management operations over a running gateway's state.

    Args:
        config: The gateway's resolved config.
        bus: Message bus; workflow runs are started by publishing to it.
        channels: The gateway's active channels.
        agent_names: Names of the registered agents (``"default"`` first).
        cron_manager: Scheduler, or ``None`` when cron is disabled.
        workflow_registry: Workflow registry, or ``None`` when workflows are off.
        workflow_run_store: Run journal, or ``None`` when not enabled.
        workflow_runtime: The workflow runtime (graph runs + reviews), or ``None``.
        live_runs: The gateway's map of run_id → task for runs it started.
        saved_store: File store for saved (JS) workflows, or ``None`` when
            file-authored workflows are unavailable.
        saved_reload_cb: Reconciles saved files into the registry; called after
            every save/delete so changes go live immediately.
        mcp_servers: Per-server MCP load report (name, transport, tools, error).
        sessions: The gateway's SessionManager (conversation → thread id).
        checkpointer: LangGraph saver holding conversation state.
    """

    def __init__(
        self,
        *,
        config: LangclawConfig,
        bus: BaseMessageBus,
        channels: Iterable[Any],
        agent_names: Iterable[str],
        cron_manager: CronManager | None = None,
        workflow_registry: WorkflowRegistry | None = None,
        workflow_run_store: Any | None = None,
        workflow_runtime: Any | None = None,
        live_runs: Mapping[str, asyncio.Task] | None = None,
        saved_store: SavedWorkflowStore | None = None,
        saved_reload_cb: Callable[[], bool] | None = None,
        mcp_servers: Iterable[Mapping[str, Any]] | None = None,
        sessions: Any | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._channels = list(channels)
        self._agent_names = list(agent_names)
        self._cron = cron_manager
        self._registry = workflow_registry
        self._run_store = workflow_run_store
        self._runtime = workflow_runtime
        self._live_runs = live_runs if live_runs is not None else {}
        self._saved_store = saved_store
        self._saved_reload_cb = saved_reload_cb
        self._mcp_servers = [dict(m) for m in (mcp_servers or [])]
        self._sessions = sessions
        self._checkpointer = checkpointer

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return the gateway's channels, agents, model, and enabled features."""
        from langclaw import __version__

        return {
            "version": __version__,
            "model": self._config.agents.model,
            "channels": [ch.name for ch in self._channels],
            "agents": list(self._agent_names),
            "features": {
                "workflows": self._registry is not None,
                "saved_workflows": self._saved_store is not None,
                "run_journal": self._run_store is not None,
                "schedules": self._cron is not None,
                "interpreter": bool(self._config.interpreter.enabled),
                "mcp": bool(self._mcp_servers),
            },
            "mcp_servers": self._mcp_servers,
        }

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------

    async def history(self, channel: str, user_id: str, context_id: str) -> list[dict[str, str]]:
        """Return a conversation's user/assistant messages from the checkpointer.

        Tool calls and tool results are omitted; empty when the thread has none.
        """
        if self._sessions is None or self._checkpointer is None:
            raise FeatureDisabledError("Conversation history needs a checkpointer.")
        config = await self._sessions.get_config(channel, user_id, context_id)
        saved = await self._checkpointer.aget_tuple(config)
        if saved is None:
            return []
        messages = saved.checkpoint.get("channel_values", {}).get("messages", [])
        out: list[dict[str, str]] = []
        for m in messages:
            role = {"human": "user", "ai": "assistant"}.get(getattr(m, "type", ""))
            text = _message_text(getattr(m, "content", ""))
            if role and text:
                out.append({"role": role, "content": text})
        return out

    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------

    def require_workflows(self) -> WorkflowRegistry:
        """Return the registry, or raise :class:`FeatureDisabledError`."""
        if self._registry is None:
            raise FeatureDisabledError(_WORKFLOWS_OFF)
        return self._registry

    def list_workflows(self) -> list[dict[str, Any]]:
        """Return every registered workflow (without scripts)."""
        return [self._describe(spec) for spec in self.require_workflows().specs()]

    def get_workflow(self, name: str) -> dict[str, Any]:
        """Return one workflow; saved workflows include their ``script``."""
        spec = self.require_workflows().get(name)
        if spec is None:
            raise NotFoundError(f"Unknown workflow {name!r}.")
        described = self._describe(spec)
        if described["editable"]:
            described["script"] = spec.script
        return described

    def save_workflow(
        self,
        name: str,
        *,
        script: str,
        description: str = "",
        uses_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or overwrite saved workflow *name* and make it live.

        Raises:
            FeatureDisabledError: File-authored workflows are unavailable.
            ValueError: Invalid name, or *name* belongs to an in-code workflow.
        """
        registry = self.require_workflows()
        store = self._require_saved_store()
        existing = registry.get(name)
        if existing is not None and getattr(existing, "mode", "python") != "saved":
            raise ValueError(f"{name!r} is an in-code workflow; saved workflows cannot replace it.")
        store.save(name, script=script, description=description, uses_tools=uses_tools)
        self._reload_saved()
        spec = registry.get(name)
        if spec is None:
            return {"name": name, "description": description, "mode": "saved", "editable": True}
        return self.get_workflow(name)

    def delete_workflow(self, name: str) -> bool:
        """Delete saved workflow *name*. Returns ``True`` when a file was removed."""
        self.require_workflows()
        store = self._require_saved_store()
        if not store.delete(name):
            raise NotFoundError(f"No saved workflow {name!r}.")
        self._reload_saved()
        return True

    async def start_workflow(
        self,
        name: str,
        workflow_input: str,
        *,
        channel: str,
        user_id: str,
        context_id: str,
        chat_id: str = "",
    ) -> str:
        """Start a run by publishing an ``origin="workflow"`` message.

        Progress and the final output are delivered to *channel* like any other
        workflow run. Returns the new ``run_id``.
        """
        if self.require_workflows().get(name) is None:
            raise NotFoundError(f"Unknown workflow {name!r}.")
        run_id = f"{name}:{uuid.uuid4().hex[:12]}"
        await self._bus.publish(
            InboundMessage(
                channel=channel,
                user_id=user_id,
                context_id=context_id,
                chat_id=chat_id,
                content=f"run workflow {name}",
                origin="workflow",
                metadata={
                    "workflow_name": name,
                    "workflow_input": workflow_input,
                    "run_id": run_id,
                },
            )
        )
        return run_id

    async def list_runs(self, limit: int = 20, *, workflow: str = "") -> dict[str, Any]:
        """Return recent runs: graph runs (newest first), then journaled legacy runs."""
        self.require_workflows()
        runs: list[dict[str, Any]] = []
        graph = self._graph_runner()
        if graph is not None:
            for record in await graph.index.list(workflow=workflow, limit=limit):
                runs.append(self._describe_graph_run(record))
        if self._run_store is not None:
            for record in (await self._run_store.list_all())[-limit:]:
                if not workflow or record.get("spec_name") == workflow:
                    runs.append(self._describe_run(record))
        return {
            "journal_enabled": graph is not None or self._run_store is not None,
            "runs": runs[:limit],
        }

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """One graph run with its checkpointed state and per-step results."""
        self.require_workflows()
        graph = self._graph_runner()
        record = await graph.index.get(run_id) if graph is not None else None
        if record is None:
            raise NotFoundError(f"Unknown run {run_id!r}.")
        spec = self.require_workflows().get(record.get("workflow", ""))
        run = await graph.get_run(spec, run_id)
        return {**(run or record), "live": run_id in self._live_runs}

    # ------------------------------------------------------------------
    # Human review (graph workflows)
    # ------------------------------------------------------------------

    async def list_reviews(self, workflow: str = "") -> list[dict[str, Any]]:
        """Every review waiting for an answer, oldest first."""
        self.require_workflows()
        graph = self._graph_runner()
        if graph is None:
            return []
        return await graph.index.pending_reviews(workflow=workflow)

    async def answer_review(
        self,
        run_id: str,
        decision: Mapping[str, Any] | str,
        *,
        by: str,
        via: str,
        interrupt_id: str = "",
        fallback_target: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Answer a paused run's review; the first answer wins.

        The answer is recorded immediately (so every surface — Telegram, UI,
        command — sees it at once), then the run continues on the bus worker
        and delivers its result to the channel that started it.

        Args:
            run_id: The waiting run.
            decision: ``"approve"`` / ``"reject"``, or ``{"action", "data", "comment"}``.
            by: Who answered (user id or name), recorded on the review.
            via: Where it was answered (``"telegram"``, ``"ui"``...).
            interrupt_id: Which review, when a run has several; empty ⇒ oldest.
            fallback_target: Where to deliver the result if the run has no
                recorded origin (``channel``, ``user_id``, ``context_id``, ``chat_id``).

        Returns:
            The claimed review (with its ``decision``).

        Raises:
            NotFoundError: unknown run.
            ValueError: malformed decision, or no pending review / already answered
                (a :class:`ReviewAlreadyResolved`, whose message names who answered).
        """
        self.require_workflows()
        graph = self._graph_runner()
        record = await graph.index.get(run_id) if graph is not None else None
        if record is None:
            raise NotFoundError(f"Unknown run {run_id!r}.")
        if isinstance(decision, str):
            decision = {"action": decision}
        from langclaw.workflows.graph import ReviewAlreadyResolved

        try:
            review = await graph.claim_review(
                run_id, {**decision, "by": by, "via": via}, interrupt_id=interrupt_id
            )
        except ReviewAlreadyResolved as exc:
            raise ValueError(str(exc)) from exc
        target = dict(record.get("reply_to") or fallback_target or {})
        if not target.get("channel"):
            raise ValueError(f"Run {run_id} has no channel to continue on.")
        await self._bus.publish(
            InboundMessage(
                channel=target["channel"],
                user_id=target.get("user_id", ""),
                context_id=target.get("context_id", "default"),
                chat_id=target.get("chat_id", ""),
                content=f"continue workflow {record.get('workflow')}",
                origin="workflow",
                metadata={
                    "workflow_name": record.get("workflow", ""),
                    "run_id": run_id,
                    "review": review,
                },
            )
        )
        return review

    def _graph_runner(self) -> Any | None:
        return getattr(self._runtime, "graph_runner", None) if self._runtime else None

    async def run_status(self, run_id: str) -> dict[str, Any]:
        """Return a run's journaled status and whether it is live in this gateway."""
        self.require_workflows()
        live = run_id in self._live_runs
        graph = self._graph_runner()
        if graph is not None and (record := await graph.index.get(run_id)) is not None:
            return {**self._describe_graph_run(record), "live": live}
        if self._run_store is not None:
            for record in await self._run_store.list_all():
                if record.get("run_id") == run_id:
                    return {**self._describe_run(record), "live": live}
        if live:
            return {"run_id": run_id, "status": "running", "live": True}
        raise NotFoundError(f"Unknown run {run_id!r}.")

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a run this gateway started (only those are cancelable)."""
        self.require_workflows()
        task = self._live_runs.get(run_id)
        if task is None:
            raise NotFoundError(
                f"No live run {run_id} to cancel (only gateway-started runs are cancelable)."
            )
        task.cancel()
        return True

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------

    def require_schedules(self) -> CronManager:
        """Return the cron manager, or raise :class:`FeatureDisabledError`."""
        if self._cron is None:
            raise FeatureDisabledError(_SCHEDULES_OFF)
        return self._cron

    async def list_schedules(self) -> list[dict[str, Any]]:
        """Return every scheduled job."""
        return [dataclasses.asdict(job) for job in await self.require_schedules().list_jobs()]

    async def add_schedule(
        self,
        *,
        name: str,
        channel: str,
        user_id: str,
        message: str = "",
        chat_id: str = "",
        context_id: str = "default",
        cron_expr: str | None = None,
        every_seconds: int | None = None,
        agent_name: str = "",
        workflow_name: str = "",
        workflow_input: str = "",
    ) -> str:
        """Schedule a prompt (*message*) or a saved/registered workflow.

        Results are delivered to *channel* / *chat_id* (defaults to *user_id*).
        Returns the job id.
        """
        cron = self.require_schedules()
        active = [ch.name for ch in self._channels]
        if channel not in active:
            raise ValueError(f"Unknown channel {channel!r}; active channels: {active}.")
        if not message and not workflow_name:
            raise ValueError("Provide a message or workflow_name.")
        if not cron_expr and not every_seconds:
            raise ValueError("Provide cron_expr or every_seconds.")
        if workflow_name and self.require_workflows().get(workflow_name) is None:
            raise NotFoundError(f"Unknown workflow {workflow_name!r}.")
        return await cron.add_job(
            name=name,
            message=message or f"run workflow {workflow_name}",
            channel=channel,
            user_id=user_id,
            context_id=context_id,
            chat_id=chat_id or user_id,
            cron_expr=cron_expr or None,
            every_seconds=every_seconds or None,
            agent_name=agent_name,
            workflow_name=workflow_name,
            workflow_input=workflow_input,
        )

    async def remove_schedule(self, job_id: str) -> bool:
        """Remove a scheduled job."""
        if not await self.require_schedules().remove_job(job_id):
            raise NotFoundError(f"Unknown schedule {job_id!r}.")
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_saved_store(self) -> SavedWorkflowStore:
        if self._saved_store is None:
            raise FeatureDisabledError(_SAVED_OFF)
        return self._saved_store

    def _reload_saved(self) -> None:
        if self._saved_reload_cb is not None:
            self._saved_reload_cb()

    @staticmethod
    def _describe(spec: Any) -> dict[str, Any]:
        mode = getattr(spec, "mode", "python")
        return {
            "name": spec.name,
            "description": spec.description or "",
            "mode": mode,
            "editable": mode == "saved",
            # graph workflows: "file" (workflows/<name>.graph.json) or "code".
            "source": (
                ("file" if getattr(spec, "graph_spec", None) is not None else "code")
                if mode == "graph"
                else ("file" if mode == "saved" else "code")
            ),
        }

    @staticmethod
    def _describe_graph_run(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "run_id": record.get("run_id"),
            "workflow": record.get("workflow"),
            "status": record.get("status"),
            "trigger": record.get("trigger", ""),
            "started_at": record.get("started_at", ""),
            "updated_at": record.get("updated_at", ""),
            "error": record.get("error", ""),
            "pending_reviews": sum(
                1 for r in record.get("reviews", []) if r.get("decision") is None
            ),
        }

    @staticmethod
    def _describe_run(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "run_id": record.get("run_id"),
            "workflow": record.get("spec_name"),
            "status": record.get("status"),
        }


def _message_text(content: Any) -> str:
    """Flatten LangChain message content (str or content blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return ""
