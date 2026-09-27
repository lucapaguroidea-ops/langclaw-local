"""ApiChannel: token-authed HTTP control plane (chat turns, workflows, schedules)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from langclaw.bus.base import InboundMessage, OutboundMessage  # noqa: E402
from langclaw.config.schema import ApiChannelConfig, LangclawConfig  # noqa: E402
from langclaw.gateway.api import ApiChannel  # noqa: E402
from langclaw.gateway.control import ControlPlane  # noqa: E402
from langclaw.workflows import WorkflowRegistry, WorkflowSpec  # noqa: E402

TOKEN = "test-token-123456"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _registry() -> WorkflowRegistry:
    from tests.test_workflows import _graph

    reg = WorkflowRegistry()
    reg.register(WorkflowSpec(name="echo", description="echo input", graph=_graph()))
    return reg


class _Bus:
    def __init__(self) -> None:
        self.published: list[InboundMessage] = []

    async def publish(self, msg: InboundMessage) -> None:
        self.published.append(msg)


@pytest.fixture
async def setup():
    channel = ApiChannel(ApiChannelConfig(enabled=True, token=TOKEN, user_id="admin"))
    bus = _Bus()
    channel._bus = bus
    cron = MagicMock()
    cron.add_job = AsyncMock(return_value="job-1")
    cron.list_jobs = AsyncMock(return_value=[])
    cron.remove_job = AsyncMock(return_value=True)
    plane = ControlPlane(
        config=LangclawConfig(),
        bus=bus,
        channels=[channel],
        agent_names=["default"],
        cron_manager=cron,
        workflow_registry=_registry(),
    )
    channel.set_control_plane(plane)
    router = MagicMock()
    router.dispatch = AsyncMock(return_value="pong")
    channel.set_command_router(router)

    client = TestClient(TestServer(channel.build_app()))
    await client.start_server()
    try:
        yield channel, bus, client, cron, router
    finally:
        await client.close()


async def _finish_turn(channel: ApiChannel, msg: InboundMessage, *replies: str) -> None:
    """Play the gateway's part: send replies, then signal the turn is over."""
    for reply in replies:
        await channel.send(
            OutboundMessage(
                channel="api",
                user_id=msg.user_id,
                context_id=msg.context_id,
                chat_id=msg.chat_id,
                content=reply,
            )
        )
    await channel.on_turn_complete(msg)


# --- auth ---------------------------------------------------------------------


def test_disabled_without_token() -> None:
    assert ApiChannel(ApiChannelConfig(enabled=True)).is_enabled() is False
    assert ApiChannel(ApiChannelConfig(enabled=True, token=TOKEN)).is_enabled() is True


async def test_healthz_needs_no_auth(setup) -> None:
    _, _, client, _, _ = setup
    resp = await client.get("/healthz")
    assert resp.status == 200


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
async def test_requests_without_valid_token_are_rejected(setup, headers) -> None:
    _, _, client, _, _ = setup
    resp = await client.get("/v1/status", headers=headers)
    assert resp.status == 401


# --- chat turns ---------------------------------------------------------------


async def test_chat_publishes_and_turn_completes(setup) -> None:
    channel, bus, client, _, _ = setup

    resp = await client.post("/v1/chat", json={"content": "hi", "context_id": "c1"}, headers=AUTH)
    assert resp.status == 202
    turn = await resp.json()
    assert turn["status"] == "running"

    msg = bus.published[-1]
    assert (msg.channel, msg.user_id, msg.context_id) == ("api", "admin", "c1")
    assert msg.chat_id == turn["turn_id"]

    await _finish_turn(channel, msg, "thinking…", "hello!")

    resp = await client.get(f"/v1/turns/{turn['turn_id']}", headers=AUTH)
    done = await resp.json()
    assert done["status"] == "done"
    assert [m["content"] for m in done["messages"]] == ["thinking…", "hello!"]


async def test_chat_wait_returns_completed_turn(setup) -> None:
    channel, bus, client, _, _ = setup

    async def gateway() -> None:
        while not bus.published:
            await asyncio.sleep(0.01)
        await _finish_turn(channel, bus.published[-1], "done fast")

    task = asyncio.create_task(gateway())
    resp = await client.post("/v1/chat?wait=5", json={"content": "hi"}, headers=AUTH)
    await task

    assert resp.status == 200
    turn = await resp.json()
    assert turn["status"] == "done"
    assert turn["messages"][0]["content"] == "done fast"


async def test_chat_slash_command_runs_immediately(setup) -> None:
    _, bus, client, _, router = setup

    resp = await client.post("/v1/chat", json={"content": "/ping now"}, headers=AUTH)
    turn = await resp.json()

    assert resp.status == 200
    assert turn["status"] == "done"
    assert turn["messages"] == [{"type": "command", "content": "pong", "metadata": {}}]
    assert router.dispatch.call_args[0][0] == "ping"
    assert not bus.published


async def test_chat_requires_content(setup) -> None:
    _, _, client, _, _ = setup
    resp = await client.post("/v1/chat", json={"content": "  "}, headers=AUTH)
    assert resp.status == 400


async def test_unknown_turn_is_404(setup) -> None:
    _, _, client, _, _ = setup
    resp = await client.get("/v1/turns/nope", headers=AUTH)
    assert resp.status == 404


async def test_list_turns_by_context(setup) -> None:
    channel, bus, client, _, _ = setup
    await client.post("/v1/chat", json={"content": "a", "context_id": "x"}, headers=AUTH)
    await client.post("/v1/chat", json={"content": "b", "context_id": "y"}, headers=AUTH)

    resp = await client.get("/v1/turns?context_id=x", headers=AUTH)
    turns = (await resp.json())["turns"]
    assert [t["context_id"] for t in turns] == ["x"]
    assert turns[0]["content"] == "a"


# --- control plane ------------------------------------------------------------


async def test_status(setup) -> None:
    _, _, client, _, _ = setup
    resp = await client.get("/v1/status", headers=AUTH)
    body = await resp.json()
    assert body["channels"] == ["api"]
    assert body["features"]["workflows"] is True


async def test_workflow_errors_map_to_http_status(setup) -> None:
    _, _, client, _, _ = setup
    resp = await client.get("/v1/workflows/missing", headers=AUTH)
    assert resp.status == 404
    # No workflows folder in this setup → 409 naming the setting to change.
    resp = await client.put("/v1/workflows/digest", json={"nodes": {}}, headers=AUTH)
    assert resp.status == 409
    assert "WORKFLOWS__ENABLED" in (await resp.json())["error"]


async def test_start_workflow_run_is_tracked_as_turn(setup) -> None:
    channel, bus, client, _, _ = setup

    resp = await client.post("/v1/workflows/echo/runs", json={"input": {"q": 1}}, headers=AUTH)
    assert resp.status == 202
    body = await resp.json()

    msg = bus.published[-1]
    assert msg.origin == "workflow"
    assert msg.metadata["workflow_input"] == '{"q": 1}'
    assert msg.chat_id == body["turn_id"]
    assert body["run_id"] == msg.metadata["run_id"]

    await _finish_turn(channel, msg, "Workflow echo completed")
    turn = await (await client.get(f"/v1/turns/{body['turn_id']}", headers=AUTH)).json()
    assert turn["status"] == "done"


async def test_schedules_crud(setup) -> None:
    _, _, client, cron, _ = setup

    resp = await client.post(
        "/v1/schedules",
        json={
            "name": "morning",
            "channel": "api",
            "user_id": "admin",
            "message": "news",
            "cron_expr": "0 9 * * *",
        },
        headers=AUTH,
    )
    assert resp.status == 201
    assert (await resp.json())["id"] == "job-1"

    resp = await client.get("/v1/schedules", headers=AUTH)
    assert (await resp.json())["schedules"] == []

    resp = await client.delete("/v1/schedules/job-1", headers=AUTH)
    assert resp.status == 200

    resp = await client.post("/v1/schedules", json={"name": "x"}, headers=AUTH)
    assert resp.status == 400


async def test_without_control_plane_management_is_503() -> None:
    channel = ApiChannel(ApiChannelConfig(enabled=True, token=TOKEN))
    client = TestClient(TestServer(channel.build_app()))
    await client.start_server()
    try:
        resp = await client.get("/v1/status", headers=AUTH)
        assert resp.status == 503
    finally:
        await client.close()


async def test_history_endpoint_uses_api_identity() -> None:
    channel = ApiChannel(ApiChannelConfig(enabled=True, token=TOKEN, user_id="admin"))
    plane = MagicMock()
    plane.history = AsyncMock(return_value=[{"role": "user", "content": "hi"}])
    channel.set_control_plane(plane)
    client = TestClient(TestServer(channel.build_app()))
    await client.start_server()
    try:
        resp = await client.get("/v1/history?context_id=web", headers=AUTH)
        assert resp.status == 200
        assert (await resp.json())["messages"] == [{"role": "user", "content": "hi"}]
        plane.history.assert_awaited_once_with("api", "admin", "web")
    finally:
        await client.close()


async def test_documents_routes(setup) -> None:
    channel, _bus, client, _cron, _router = setup
    off = await client.get("/v1/documents", headers=AUTH)
    assert off.status == 409 and "DOCUMENTS__ENABLED" in (await off.json())["error"]

    class Fake:
        async def list_documents(self, **kw):
            return {"documents": [], "count": 0, "mode": "semantic", "semantic": True, "kw": kw}

        async def get_document(self, key):
            return {"document": {"bucket_key": key}, "link": ""}

    plane = channel._plane
    plane.list_documents = Fake().list_documents
    plane.get_document = Fake().get_document
    resp = await client.get(
        "/v1/documents", params={"q": "rent", "semantic": "true", "limit": "5"}, headers=AUTH
    )
    body = await resp.json()
    assert body["kw"]["q"] == "rent" and body["kw"]["semantic"] is True and body["kw"]["limit"] == 5
    detail = await client.get("/v1/documents/inbox/2026-09-27/a-b.pdf", headers=AUTH)
    assert (await detail.json())["document"]["bucket_key"] == "inbox/2026-09-27/a-b.pdf"
