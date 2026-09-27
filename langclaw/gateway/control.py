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
import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from langclaw.bus.base import InboundMessage

if TYPE_CHECKING:
    import asyncio

    from langclaw.bus.base import BaseMessageBus
    from langclaw.config.schema import LangclawConfig
    from langclaw.cron.scheduler import CronManager
    from langclaw.workflows import WorkflowRegistry
    from langclaw.workflows.files import WorkflowFiles


class NotFoundError(LookupError):
    """The requested workflow, run, or schedule does not exist."""


class FeatureDisabledError(RuntimeError):
    """The operation needs a feature that is disabled in config."""


class ConflictError(RuntimeError):
    """The request conflicts with the current state (e.g. a review already answered).

    ``decision`` holds the earlier answer (who, where, what), when there is one.
    """

    def __init__(self, message: str, decision: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.decision = dict(decision or {})


_WORKFLOWS_OFF = "Workflows are disabled. Set LANGCLAW__WORKFLOWS__ENABLED=true."
_FILES_OFF = (
    "Workflow files are unavailable: the gateway was started without a workflows "
    "folder (set LANGCLAW__WORKFLOWS__ENABLED=true)."
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
        workflow_runtime: The workflow runtime (graph runs + reviews), or ``None``.
        live_runs: The gateway's map of run_id → task for runs it started.
        workflows_dir: Folder holding ``<name>.graph.json`` workflow files, or
            ``None`` when workflow files are unavailable.
        workflows_reload_cb: Reconciles the files into the registry; called after
            every save/delete so changes go live immediately.
        workflow_file_errors: Returns validation errors of files that failed to
            load, by name.
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
        workflow_runtime: Any | None = None,
        live_runs: Mapping[str, asyncio.Task] | None = None,
        workflows_dir: Path | None = None,
        workflows_reload_cb: Callable[[], bool] | None = None,
        workflow_file_errors: Callable[[], Mapping[str, list[str]]] | None = None,
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
        self._runtime = workflow_runtime
        self._live_runs = live_runs if live_runs is not None else {}
        self._workflow_file_errors = workflow_file_errors
        # One write path for workflow files, shared with the agent's manage_workflows
        # tool: the runtime's when the app built one, else one over *workflows_dir*.
        self._files = getattr(workflow_runtime, "files", None)
        if self._files is None and workflows_dir is not None:
            from langclaw.workflows.files import WorkflowFiles

            self._files = WorkflowFiles(
                workflows_dir,
                registry=workflow_registry,
                reload_cb=workflows_reload_cb,
                catalog=getattr(workflow_runtime, "catalog", None),
            )
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
                "workflow_files": self._files is not None,
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
        """Every workflow, plus workflow files that failed to load (``valid: False``)."""
        registry = self.require_workflows()
        out = [{**self._describe(spec), "valid": True} for spec in registry.specs()]
        for name, errors in sorted(self.workflow_file_errors().items()):
            if name not in registry:
                out.append(
                    {
                        "name": name,
                        "description": "",
                        "source": "file",
                        "editable": True,
                        "valid": False,
                        "errors": errors,
                    }
                )
        return out

    def get_workflow(self, name: str) -> dict[str, Any]:
        """One workflow with a Mermaid drawing; file workflows include ``graph``.

        A file that failed to load is returned too (``valid: False`` with its
        ``errors`` and raw content), so an editor can fix it.
        """
        spec = self.require_workflows().get(name)
        if spec is None:
            errors = self.workflow_file_errors().get(name)
            if errors is None or self._files is None:
                raise NotFoundError(f"Unknown workflow {name!r}.")
            return {
                "name": name,
                "description": "",
                "source": "file",
                "editable": True,
                "valid": False,
                "errors": errors,
                "graph": self._files.read(name),
            }
        described = {**self._describe(spec), "valid": True}
        if spec.graph_spec is not None:
            described["graph"] = json.loads(spec.graph_spec.to_file())
        described["mermaid"] = spec.graph.compile().get_graph().draw_mermaid()
        return described

    def workflow_file_errors(self) -> dict[str, list[str]]:
        """Validation errors of workflow files that failed to load, by name."""
        return dict(self._workflow_file_errors() if self._workflow_file_errors else {})

    def catalog(self) -> dict[str, list[str]]:
        """Tool and subagent names workflow steps can use (empty before the agent is built)."""
        found = self._runtime.catalog() if self._runtime is not None else None
        return dict(found or {"tools": [], "subagents": []})

    def validate_workflow(self, name: str, graph: Mapping[str, Any] | str) -> dict[str, Any]:
        """Check a workflow file without saving it.

        Returns:
            ``{"valid", "errors", "warnings"}`` — errors block saving; warnings
            (e.g. a tool that isn't available right now) don't.
        """
        self.require_workflows()
        return self._require_files().validate(name, graph)

    def save_workflow(self, name: str, graph: Mapping[str, Any] | str) -> dict[str, Any]:
        """Create or replace workflow file *name*, snapshot the old one, and make it live.

        Returns:
            The workflow (as :meth:`get_workflow`) plus ``created`` and ``warnings``.

        Raises:
            FeatureDisabledError: Workflow files are unavailable.
            ValueError: The graph is invalid (the message lists every problem), or
                *name* belongs to a workflow defined in code.
        """
        self.require_workflows()
        saved = self._require_files().save(name, graph)
        return {**self.get_workflow(name), **saved}

    def delete_workflow(self, name: str) -> bool:
        """Delete workflow file *name* (its history is kept for restore)."""
        from langclaw.workflows.files import WorkflowFileNotFound

        self.require_workflows()
        try:
            self._require_files().delete(name)
        except WorkflowFileNotFound as exc:
            raise NotFoundError(str(exc)) from exc
        return True

    def workflow_versions(self, name: str) -> list[dict[str, Any]]:
        """Saved snapshots of workflow file *name*, newest first."""
        self.require_workflows()
        return self._require_files().versions(name)

    def workflow_version(self, name: str, version: str) -> dict[str, Any]:
        """One snapshot's content."""
        from langclaw.workflows.files import WorkflowFileNotFound

        self.require_workflows()
        try:
            graph = self._require_files().read_version(name, version)
        except WorkflowFileNotFound as exc:
            raise NotFoundError(str(exc)) from exc
        return {"name": name, "version": version, "graph": graph}

    def restore_workflow(self, name: str, version: str) -> dict[str, Any]:
        """Make snapshot *version* current (the current file is snapshotted first)."""
        from langclaw.workflows.files import WorkflowFileNotFound

        self.require_workflows()
        try:
            self._require_files().restore(name, version)
        except WorkflowFileNotFound as exc:
            raise NotFoundError(str(exc)) from exc
        return self.get_workflow(name)

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

    async def list_runs(
        self, limit: int = 20, *, workflow: str = "", status: str = ""
    ) -> dict[str, Any]:
        """Return recent runs, newest first, optionally for one workflow / status."""
        self.require_workflows()
        graph = self._graph_runner()
        if graph is None:
            return {"runs": []}
        records = await graph.index.list(workflow=workflow, status=status, limit=limit)
        return {"runs": [self._describe_graph_run(r) for r in records]}

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
            ValueError: malformed decision.
            ConflictError: no pending review, or it was already answered (the
                message names who answered, and where).
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
            raise ConflictError(str(exc), exc.decision) from exc
        await self._mark_resolved(record, review)
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

    async def answer_review_by_key(
        self, key: str, action: str, *, by: str, via: str
    ) -> tuple[dict[str, Any], str]:
        """Answer the review a button with short *key* belongs to.

        Returns:
            ``(claimed review, run_id)``.

        Raises:
            NotFoundError / ConflictError / ValueError: as :meth:`answer_review`.
        """
        self.require_workflows()
        graph = self._graph_runner()
        found = await graph.index.find_review(key) if graph is not None else None
        if found is None:
            raise NotFoundError("That review no longer exists.")
        run_id, interrupt_id = found
        review = await self.answer_review(
            run_id, {"action": action}, by=by, via=via, interrupt_id=interrupt_id
        )
        return review, run_id

    async def review_request(self, key: str) -> dict[str, Any] | None:
        """The review request with short *key* (for re-rendering, e.g. edit help)."""
        graph = self._graph_runner()
        found = await graph.index.find_review(key) if graph is not None else None
        if found is None:
            return None
        run_id, interrupt_id = found
        record = await graph.index.get(run_id) or {}
        for review in record.get("reviews", []):
            if review["interrupt_id"] == interrupt_id:
                return _request_of(record, review)
        return None

    async def notify_review_requests(
        self, record: Mapping[str, Any], reviews: list[dict[str, Any]]
    ) -> None:
        """Send each new review request to where the run started and to the
        configured review chat (``workflows.review_channel`` / ``review_chat_id``),
        once per chat, and remember where each went."""
        graph = self._graph_runner()
        targets: list[dict[str, str]] = []
        reply_to = dict(record.get("reply_to") or {})
        if reply_to.get("channel"):
            targets.append(reply_to)
        cfg = self._config.workflows
        if cfg.review_channel and cfg.review_chat_id:
            targets.append(
                {
                    "channel": cfg.review_channel,
                    "user_id": cfg.review_chat_id,
                    "context_id": cfg.review_chat_id,
                    "chat_id": cfg.review_chat_id,
                }
            )
        seen: set[tuple[str, str]] = set()
        for target in targets:
            where = (target["channel"], target.get("chat_id") or target.get("user_id", ""))
            channel = self._channel(target["channel"])
            if where in seen or channel is None:
                continue
            seen.add(where)
            for review in reviews:
                request = _request_of(record, review)
                try:
                    ref = await channel.send_review_request(target, request)
                except Exception as exc:  # noqa: BLE001 — one channel failing must not stop others
                    logger.warning(f"Review request to {where} failed: {exc}")
                    continue
                if ref and graph is not None:
                    notice = {"channel": target["channel"], "chat_id": where[1], **ref}
                    await graph.index.add_notice(record["run_id"], review["interrupt_id"], notice)

    async def _mark_resolved(self, record: Mapping[str, Any], review: dict[str, Any]) -> None:
        request = _request_of(record, review)
        for notice in review.get("notices", []):
            channel = self._channel(notice.get("channel", ""))
            if channel is None:
                continue
            try:
                await channel.mark_review_resolved(notice, request, review["decision"])
            except Exception as exc:  # noqa: BLE001 — cosmetic; the answer is recorded
                logger.warning(f"Updating review message {notice} failed: {exc}")

    def _channel(self, name: str) -> Any | None:
        return next((c for c in self._channels if getattr(c, "name", "") == name), None)

    def _graph_runner(self) -> Any | None:
        return getattr(self._runtime, "graph_runner", None) if self._runtime else None

    async def run_status(self, run_id: str) -> dict[str, Any]:
        """Return a run's status and whether it is executing in this gateway now."""
        self.require_workflows()
        live = run_id in self._live_runs
        graph = self._graph_runner()
        if graph is not None and (record := await graph.index.get(run_id)) is not None:
            return {**self._describe_graph_run(record), "live": live}
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

    def _require_files(self) -> WorkflowFiles:
        if self._files is None:
            raise FeatureDisabledError(_FILES_OFF)
        return self._files

    @staticmethod
    def _describe(spec: Any) -> dict[str, Any]:
        return {
            "name": spec.name,
            "description": spec.description or "",
            # "file": workflows/<name>.graph.json (editable); "code": a Python graph.
            "source": spec.source,
            "editable": spec.source == "file",
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


def _request_of(record: Mapping[str, Any], review: Mapping[str, Any]) -> dict[str, Any]:
    """The channel-facing view of one review (see :mod:`langclaw.gateway.reviews`)."""
    return {
        "run_id": record["run_id"],
        "workflow": record.get("workflow", ""),
        "key": review.get("key", ""),
        "interrupt_id": review["interrupt_id"],
        "message": review.get("message", ""),
        "data": review.get("data") or {},
        "editable": review.get("editable", ""),
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
