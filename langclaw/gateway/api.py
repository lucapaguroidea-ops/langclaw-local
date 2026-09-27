"""
ApiChannel — HTTP control plane for UIs (Appsmith, Retool, a web app) and scripts.

Requires: langclaw[api]  →  uv add "langclaw[api]"

Enable with::

    LANGCLAW__CHANNELS__API__ENABLED=true
    LANGCLAW__CHANNELS__API__TOKEN=<long random secret>
    LANGCLAW__CHANNELS__API__HOST=::          # all interfaces (default 127.0.0.1)
    LANGCLAW__CHANNELS__API__PORT=18790

Every endpoint except ``GET /healthz`` requires ``Authorization: Bearer <token>``.
Responses are JSON; errors are ``{"error": "..."}`` with 400 (bad input),
401 (auth), 404 (not found), 409 (feature disabled — the message names the
setting — or a review already answered, with ``decision``), or 503 (gateway not
ready).

Chat is turn-based because an agent turn (with tool calls) can outlast an HTTP
client's timeout::

    POST /v1/chat            {"content", "context_id"?, "agent_name"?}  [?wait=<sec>]
                             → 202 turn (running), or 200 turn if done within wait
    GET  /v1/turns/{id}      → {"turn_id", "status": "running"|"done", "messages": [...]}
    GET  /v1/turns           [?context_id=&limit=]  recent turns (in memory)
    GET  /v1/history         [?context_id=]  saved conversation (survives restarts)

``content`` starting with ``/`` runs a chat command (``/help``, ``/workflows``…)
and returns a completed turn immediately.

Management (see :class:`~langclaw.gateway.control.ControlPlane`)::

    GET    /v1/status                    GET /v1/catalog  (tools/subagents for steps)
    GET    /v1/workflows                 GET/PUT/DELETE /v1/workflows/{name}
                                         (PUT body: the .graph.json content)
    POST   /v1/workflows/{name}/validate {graph} → {"valid", "errors", "warnings"}
    GET    /v1/workflows/{name}/versions[/{version}]
    POST   /v1/workflows/{name}/versions/{version}/restore
    POST   /v1/workflows/{name}/runs     {"input"?}  → 202 {"run_id", "turn_id"}
    GET    /v1/workflows/{name}/runs     GET /v1/runs [?workflow=&status=&limit=]
    GET    /v1/runs/{run_id}             status, reviews, state, and each step's result
    POST   /v1/runs/{run_id}/cancel
    GET    /v1/reviews [?workflow=]      reviews waiting for an answer
    POST   /v1/runs/{run_id}/review      {"action": "approve"|"edit"|"reject",
                                          "data"?, "comment"?, "interrupt_id"?, "by"?, "via"?}
    GET    /v1/schedules                 POST /v1/schedules   DELETE /v1/schedules/{id}

A workflow run started here is tracked as a turn: poll ``/v1/turns/{turn_id}``
for its progress lines and final output.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from langclaw.bus.base import InboundMessage
from langclaw.gateway.base import BaseChannel
from langclaw.gateway.commands import CommandContext
from langclaw.gateway.control import ConflictError, FeatureDisabledError, NotFoundError

if TYPE_CHECKING:
    from aiohttp import web

    from langclaw.bus.base import BaseMessageBus, OutboundMessage
    from langclaw.config.schema import ApiChannelConfig
    from langclaw.gateway.control import ControlPlane

_MAX_WAIT_SECONDS = 120.0
# Tool output can be large; the UI only needs enough to show what happened.
_MAX_TOOL_RESULT_CHARS = 4000

Handler = Callable[["web.Request"], Awaitable["web.StreamResponse"]]


class _Turn:
    """One request/response exchange: a chat message or a workflow run."""

    __slots__ = ("turn_id", "context_id", "content", "status", "messages", "created", "done")

    def __init__(self, turn_id: str, context_id: str, content: str) -> None:
        self.turn_id = turn_id
        self.context_id = context_id
        self.content = content
        self.status = "running"
        self.messages: list[dict[str, Any]] = []
        self.created = time.time()
        self.done = asyncio.Event()

    def finish(self) -> None:
        self.status = "done"
        self.done.set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "context_id": self.context_id,
            "content": self.content,
            "status": self.status,
            "messages": self.messages,
            "created_at": self.created,
        }


class ApiChannel(BaseChannel):
    """HTTP channel exposing chat turns and the gateway's :class:`ControlPlane`."""

    name = "api"

    def __init__(self, config: ApiChannelConfig) -> None:
        self._config = config
        self._bus: BaseMessageBus | None = None
        self._plane: ControlPlane | None = None
        self._turns: OrderedDict[str, _Turn] = OrderedDict()
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------
    # BaseChannel
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return self._config.enabled and bool(self._config.token)

    def set_control_plane(self, plane: ControlPlane) -> None:
        self._plane = plane

    async def start(self, bus: BaseMessageBus) -> None:
        try:
            from aiohttp import web
        except ImportError as exc:
            raise ImportError(
                "ApiChannel requires 'langclaw[api]'. Install with: uv add 'langclaw[api]'"
            ) from exc

        self._bus = bus
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        logger.info(f"API channel listening on {self._config.host}:{self._config.port}")
        await asyncio.Event().wait()  # run until cancelled

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def send_ai_message(self, msg: OutboundMessage) -> None:
        self._record(msg, {"type": "ai", "content": msg.content, "metadata": {}})

    async def send_review_request(
        self, target: dict[str, str], request: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Record the review as a structured ``review`` message on the run's turn,
        so a UI can show it (and answer via ``POST /v1/runs/{id}/review``)."""
        from langclaw.bus.base import OutboundMessage

        msg = OutboundMessage(
            channel=self.name,
            user_id=target.get("user_id", ""),
            context_id=target.get("context_id", "default"),
            chat_id=target.get("chat_id", ""),
            content=request.get("message", ""),
            type="ai",
        )
        self._record(msg, {"type": "review", "content": msg.content, "metadata": dict(request)})
        return None

    async def send_tool_progress(self, msg: OutboundMessage) -> None:
        meta = msg.metadata or {}
        self._record(
            msg,
            {
                "type": "tool_progress",
                "content": msg.content,
                "metadata": {k: meta[k] for k in ("tool", "args", "tool_call_id") if k in meta},
            },
        )

    async def send_tool_result(self, msg: OutboundMessage) -> None:
        meta = msg.metadata or {}
        self._record(
            msg,
            {
                "type": "tool_result",
                "content": msg.content[:_MAX_TOOL_RESULT_CHARS],
                "metadata": {k: meta[k] for k in ("tool_call_id",) if k in meta},
            },
        )

    async def on_turn_complete(self, msg: InboundMessage) -> None:
        turn = self._turns.get(msg.chat_id)
        if turn is not None:
            turn.finish()

    # ------------------------------------------------------------------
    # HTTP app
    # ------------------------------------------------------------------

    def build_app(self) -> web.Application:
        """Return the aiohttp application (used by :meth:`start` and in tests)."""
        from aiohttp import web

        app = web.Application(middlewares=[self._auth_middleware, self._error_middleware])
        app.add_routes(
            [
                web.get("/healthz", self._healthz),
                web.get("/v1/status", self._status),
                web.post("/v1/chat", self._chat),
                web.get("/v1/turns", self._list_turns),
                web.get("/v1/turns/{turn_id}", self._get_turn),
                web.get("/v1/history", self._history),
                web.get("/v1/catalog", self._catalog),
                web.get("/v1/workflows", self._list_workflows),
                web.get("/v1/workflows/{name}", self._get_workflow),
                web.put("/v1/workflows/{name}", self._save_workflow),
                web.delete("/v1/workflows/{name}", self._delete_workflow),
                web.post("/v1/workflows/{name}/validate", self._validate_workflow),
                web.get("/v1/workflows/{name}/versions", self._list_versions),
                web.get("/v1/workflows/{name}/versions/{version}", self._get_version),
                web.post("/v1/workflows/{name}/versions/{version}/restore", self._restore_version),
                web.post("/v1/workflows/{name}/runs", self._start_run),
                web.get("/v1/workflows/{name}/runs", self._list_workflow_runs),
                web.get("/v1/runs", self._list_runs),
                web.get("/v1/runs/{run_id}", self._get_run),
                web.post("/v1/runs/{run_id}/cancel", self._cancel_run),
                web.post("/v1/runs/{run_id}/review", self._answer_review),
                web.get("/v1/reviews", self._list_reviews),
                web.get("/v1/documents", self._list_documents),
                web.get("/v1/documents/{key:.+}", self._get_document),
                web.get("/v1/schedules", self._list_schedules),
                web.post("/v1/schedules", self._add_schedule),
                web.delete("/v1/schedules/{job_id}", self._remove_schedule),
            ]
        )
        return app

    @staticmethod
    def _json(data: Any, status: int = 200) -> web.Response:
        from aiohttp import web

        return web.json_response(data, status=status)

    @property
    def _auth_middleware(self) -> Any:
        from aiohttp import web

        expected = f"Bearer {self._config.token}".encode()

        @web.middleware
        async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
            if request.path == "/healthz":
                return await handler(request)
            given = request.headers.get("Authorization", "").encode()
            if not self._config.token or not hmac.compare_digest(given, expected):
                return self._json({"error": "unauthorized"}, 401)
            return await handler(request)

        return middleware

    @property
    def _error_middleware(self) -> Any:
        from aiohttp import web

        @web.middleware
        async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except NotFoundError as exc:
                return self._json({"error": str(exc)}, 404)
            except FeatureDisabledError as exc:
                return self._json({"error": str(exc)}, 409)
            except ConflictError as exc:
                return self._json({"error": str(exc), "decision": exc.decision}, 409)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception:
                logger.exception(f"API error on {request.method} {request.path}")
                return self._json({"error": "internal error"}, 500)

        return middleware

    def _require_plane(self) -> ControlPlane:
        from aiohttp import web

        if self._plane is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "gateway not ready"}), content_type="application/json"
            )
        return self._plane

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be JSON.") from exc
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    # ------------------------------------------------------------------
    # Handlers — health, status, chat
    # ------------------------------------------------------------------

    async def _healthz(self, request: web.Request) -> web.Response:
        return self._json({"ok": True})

    async def _status(self, request: web.Request) -> web.Response:
        return self._json(self._require_plane().status())

    async def _chat(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("'content' is required.")
        context_id = str(body.get("context_id") or "default")
        wait = _parse_wait(request.query.get("wait"))
        turn = self._new_turn(context_id, content)
        stripped = content.strip()

        if stripped.startswith("/") and self._command_router is not None:
            parts = stripped.split()
            ctx = CommandContext(
                channel=self.name,
                user_id=self._config.user_id,
                context_id=context_id,
                chat_id=turn.turn_id,
                args=parts[1:],
                display_name=self._config.user_id,
            )
            reply = await self._command_router.dispatch(parts[0].lstrip("/").lower(), ctx)
            turn.messages.append({"type": "command", "content": reply, "metadata": {}})
            turn.finish()
            return self._json(turn.to_dict())

        if self._bus is None:
            raise FeatureDisabledError("Gateway bus not started.")
        metadata = {"agent_name": body["agent_name"]} if body.get("agent_name") else {}
        await self._bus.publish(
            InboundMessage(
                channel=self.name,
                user_id=self._config.user_id,
                context_id=context_id,
                chat_id=turn.turn_id,
                content=content,
                origin="channel",
                metadata=metadata,
            )
        )
        return await self._respond_with_turn(turn, wait)

    async def _list_turns(self, request: web.Request) -> web.Response:
        context_id = request.query.get("context_id")
        limit = _parse_int(request.query.get("limit"), default=50, name="limit")
        turns = [t for t in self._turns.values() if context_id in (None, t.context_id)]
        return self._json({"turns": [t.to_dict() for t in turns[-limit:]]})

    async def _history(self, request: web.Request) -> web.Response:
        context_id = request.query.get("context_id") or "default"
        messages = await self._require_plane().history(self.name, self._config.user_id, context_id)
        return self._json({"context_id": context_id, "messages": messages})

    async def _get_turn(self, request: web.Request) -> web.Response:
        turn = self._turns.get(request.match_info["turn_id"])
        if turn is None:
            raise NotFoundError("Unknown turn (it may have expired).")
        wait = _parse_wait(request.query.get("wait"))
        return await self._respond_with_turn(turn, wait, running_status=200)

    # ------------------------------------------------------------------
    # Handlers — workflows & runs
    # ------------------------------------------------------------------

    async def _list_workflows(self, request: web.Request) -> web.Response:
        return self._json({"workflows": self._require_plane().list_workflows()})

    async def _get_workflow(self, request: web.Request) -> web.Response:
        return self._json(self._require_plane().get_workflow(request.match_info["name"]))

    async def _save_workflow(self, request: web.Request) -> web.Response:
        # The body is the workflow file itself (see docs/guides/workflows.md);
        # an invalid graph is a 400 listing every problem.
        body = await self._body(request)
        return self._json(self._require_plane().save_workflow(request.match_info["name"], body))

    async def _delete_workflow(self, request: web.Request) -> web.Response:
        self._require_plane().delete_workflow(request.match_info["name"])
        return self._json({"deleted": True})

    async def _start_run(self, request: web.Request) -> web.Response:
        plane = self._require_plane()
        body = await self._body(request)
        name = request.match_info["name"]
        raw_input = body.get("input", "")
        wf_input = raw_input if isinstance(raw_input, str) else json.dumps(raw_input)
        context_id = str(body.get("context_id") or "workflows")
        turn = self._new_turn(context_id, f"run workflow {name}")
        try:
            run_id = await plane.start_workflow(
                name,
                wf_input,
                channel=self.name,
                user_id=self._config.user_id,
                context_id=context_id,
                chat_id=turn.turn_id,
            )
        except Exception:
            self._turns.pop(turn.turn_id, None)
            raise
        return self._json({"run_id": run_id, "turn_id": turn.turn_id}, 202)

    async def _catalog(self, request: web.Request) -> web.Response:
        return self._json(self._require_plane().catalog())

    async def _validate_workflow(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        return self._json(self._require_plane().validate_workflow(request.match_info["name"], body))

    async def _list_versions(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return self._json({"name": name, "versions": self._require_plane().workflow_versions(name)})

    async def _get_version(self, request: web.Request) -> web.Response:
        info = request.match_info
        return self._json(self._require_plane().workflow_version(info["name"], info["version"]))

    async def _restore_version(self, request: web.Request) -> web.Response:
        info = request.match_info
        return self._json(self._require_plane().restore_workflow(info["name"], info["version"]))

    async def _list_runs(self, request: web.Request) -> web.Response:
        limit = _parse_int(request.query.get("limit"), default=20, name="limit")
        return self._json(
            await self._require_plane().list_runs(
                limit=limit,
                workflow=request.query.get("workflow", ""),
                status=request.query.get("status", ""),
            )
        )

    async def _list_workflow_runs(self, request: web.Request) -> web.Response:
        plane = self._require_plane()
        name = request.match_info["name"]
        plane.get_workflow(name)  # 404 for an unknown workflow
        limit = _parse_int(request.query.get("limit"), default=20, name="limit")
        return self._json(
            await plane.list_runs(
                limit=limit, workflow=name, status=request.query.get("status", "")
            )
        )

    async def _get_run(self, request: web.Request) -> web.Response:
        return self._json(await self._require_plane().get_run(request.match_info["run_id"]))

    async def _list_reviews(self, request: web.Request) -> web.Response:
        reviews = await self._require_plane().list_reviews(request.query.get("workflow", ""))
        return self._json({"reviews": reviews})

    async def _answer_review(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        action = body.get("action")
        if not isinstance(action, str) or not action:
            raise ValueError("'action' is required: approve, edit, or reject.")
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("'data' must be an object.")
        review = await self._require_plane().answer_review(
            request.match_info["run_id"],
            {"action": action, "data": data, "comment": str(body.get("comment") or "")},
            by=str(body.get("by") or self._config.user_id),
            via=str(body.get("via") or self.name),
            interrupt_id=str(body.get("interrupt_id") or ""),
            fallback_target={
                "channel": self.name,
                "user_id": self._config.user_id,
                "context_id": "workflows",
                "chat_id": "",
            },
        )
        return self._json({"review": review, "continuing": True})

    async def _cancel_run(self, request: web.Request) -> web.Response:
        self._require_plane().cancel_run(request.match_info["run_id"])
        return self._json({"cancelling": True})

    # ------------------------------------------------------------------
    # Handlers — schedules
    # ------------------------------------------------------------------

    async def _list_documents(self, request: web.Request) -> web.Response:
        query = request.query
        return self._json(
            await self._require_plane().list_documents(
                q=query.get("q", ""),
                semantic=query.get("semantic", "").lower() in ("1", "true", "yes"),
                sender=query.get("sender", ""),
                receiver=query.get("receiver", ""),
                doc_type=query.get("doc_type", ""),
                date_from=query.get("date_from", ""),
                date_to=query.get("date_to", ""),
                status=query.get("status", ""),
                limit=_parse_int(query.get("limit"), default=50, name="limit"),
            )
        )

    async def _get_document(self, request: web.Request) -> web.Response:
        return self._json(await self._require_plane().get_document(request.match_info["key"]))

    async def _list_schedules(self, request: web.Request) -> web.Response:
        return self._json({"schedules": await self._require_plane().list_schedules()})

    async def _add_schedule(self, request: web.Request) -> web.Response:
        plane = self._require_plane()
        body = await self._body(request)
        for field in ("name", "channel", "user_id"):
            if not isinstance(body.get(field), str) or not body[field].strip():
                raise ValueError(f"'{field}' is required.")
        every = body.get("every_seconds")
        if every is not None and (not isinstance(every, int) or every <= 0):
            raise ValueError("'every_seconds' must be a positive integer.")
        raw_input = body.get("workflow_input", "")
        job_id = await plane.add_schedule(
            name=body["name"],
            channel=body["channel"],
            user_id=body["user_id"],
            message=str(body.get("message") or ""),
            chat_id=str(body.get("chat_id") or ""),
            context_id=str(body.get("context_id") or "default"),
            cron_expr=body.get("cron_expr") or None,
            every_seconds=every,
            agent_name=str(body.get("agent_name") or ""),
            workflow_name=str(body.get("workflow_name") or ""),
            workflow_input=raw_input if isinstance(raw_input, str) else json.dumps(raw_input),
        )
        return self._json({"id": job_id}, 201)

    async def _remove_schedule(self, request: web.Request) -> web.Response:
        await self._require_plane().remove_schedule(request.match_info["job_id"])
        return self._json({"deleted": True})

    # ------------------------------------------------------------------
    # Turn bookkeeping
    # ------------------------------------------------------------------

    def _new_turn(self, context_id: str, content: str, turn_id: str | None = None) -> _Turn:
        turn = _Turn(turn_id or uuid.uuid4().hex, context_id, content)
        self._turns[turn.turn_id] = turn
        while len(self._turns) > max(1, self._config.max_turns):
            self._turns.popitem(last=False)
        return turn

    def _record(self, msg: OutboundMessage, entry: dict[str, Any]) -> None:
        turn = self._turns.get(msg.chat_id)
        if turn is None:
            # Unsolicited delivery (e.g. a schedule targeting the api channel):
            # keep it as its own turn so a UI can still find it via /v1/turns.
            turn = self._new_turn(msg.context_id, "", turn_id=msg.chat_id)
        turn.messages.append(entry)

    async def _respond_with_turn(
        self, turn: _Turn, wait: float, running_status: int = 202
    ) -> web.Response:
        if wait > 0 and not turn.done.is_set():
            try:
                await asyncio.wait_for(turn.done.wait(), timeout=wait)
            except TimeoutError:
                pass
        status = 200 if turn.done.is_set() else running_status
        return self._json(turn.to_dict(), status)


def _parse_wait(raw: str | None) -> float:
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("'wait' must be a number of seconds.") from exc
    return max(0.0, min(value, _MAX_WAIT_SECONDS))


def _parse_int(raw: str | None, *, default: int, name: str) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"'{name}' must be an integer.") from exc
    if value <= 0:
        raise ValueError(f"'{name}' must be positive.")
    return value
