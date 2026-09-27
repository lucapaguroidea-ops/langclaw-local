"""Graph workflows through the gateway: /workflows run → review → approve → output."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from langclaw.bus.base import InboundMessage
from langclaw.config.schema import LangclawConfig, WorkflowsConfig
from langclaw.gateway.base import BaseChannel
from langclaw.gateway.commands import CommandContext
from langclaw.gateway.manager import GatewayManager
from langclaw.workflows import WorkflowRegistry, WorkflowRuntime
from tests.test_graph_workflows import DOC_FLOW, FakeExecutor, graph_spec_of


class _FakeChannel(BaseChannel):
    """A text-only channel: review requests arrive as text (the BaseChannel default)."""

    name = "telegram"

    def __init__(self) -> None:
        self.sent: list = []

    def is_enabled(self) -> bool:
        return True

    async def start(self, bus) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None

    async def send_ai_message(self, m) -> None:
        self.sent.append(m)

    async def send_tool_progress(self, m) -> None:
        self.sent.append(m)


class _Bus:
    """Collects published messages so the test can hand them to the manager."""

    def __init__(self) -> None:
        self.published: list[InboundMessage] = []

    async def publish(self, msg: InboundMessage) -> None:
        self.published.append(msg)


def _setup(confidence: float) -> tuple[GatewayManager, _Bus, FakeExecutor]:
    registry = WorkflowRegistry()
    registry.register(graph_spec_of("doc_flow", DOC_FLOW))
    runtime = WorkflowRuntime(WorkflowsConfig(enabled=True))
    ex = FakeExecutor(confidence=confidence)
    runtime.set_executor_factory(lambda _rt: ex)
    bus = _Bus()
    checkpointer = MagicMock()
    checkpointer.get.return_value = MagicMock()
    mgr = GatewayManager(
        config=LangclawConfig(),
        bus=bus,
        checkpointer_backend=checkpointer,
        agent=MagicMock(),
        channels=[_FakeChannel()],
        workflow_runtime=runtime,
        workflow_registry=registry,
    )
    return mgr, bus, ex


def _cmd(*args: str, channel: str = "telegram", user: str = "42") -> CommandContext:
    return CommandContext(
        channel=channel, user_id=user, context_id="c1", chat_id="c1", args=list(args)
    )


async def _run_command(mgr: GatewayManager, *args: str, **kw: Any) -> str:
    return await mgr._command_router._commands["workflows"].handler(_cmd(*args, **kw))


async def _drain(mgr: GatewayManager, bus: _Bus) -> None:
    while bus.published:
        await mgr._handle(bus.published.pop(0))


def _texts(mgr: GatewayManager) -> list[str]:
    return [m.content for m in mgr._channel_map["telegram"].sent]


async def test_run_pauses_for_review_and_approve_delivers_output() -> None:
    mgr, bus, ex = _setup(confidence=0.3)

    reply = await _run_command(mgr, "run", "doc_flow", '{"key": "inv.pdf"}')
    assert "Started workflow 'doc_flow'" in reply
    await _drain(mgr, bus)
    assert "⏸ Review needed — doc_flow" in _texts(mgr)[-1]
    assert "/workflows approve doc_flow:" in _texts(mgr)[-1]
    reviews = await mgr._control_plane.list_reviews()
    assert len(reviews) == 1
    run_id = reviews[0]["run_id"]
    assert "Waiting for review (1)" in await _run_command(mgr, "reviews")

    assert "Approved" in await _run_command(mgr, "approve", run_id)
    # First answer wins — a second answer (e.g. from the UI) is refused.
    late = await _run_command(mgr, "reject", run_id, channel="api", user="ui")
    assert "already approved by 42 via telegram" in late

    await _drain(mgr, bus)
    final = json.loads(_texts(mgr)[-1])
    assert final == {"saved": {"sender": "ACME", "confidence": 0.3}}
    assert ex.tools_called() == ["bucket_read", "documents_insert"]
    assert await mgr._control_plane.list_reviews() == []

    run = await mgr._control_plane.get_run(run_id)
    assert run["status"] == "completed"
    assert run["trigger"] == "telegram"
    decision = run["reviews"][0]["decision"]
    assert (decision["action"], decision["by"], decision["via"]) == ("approve", "42", "telegram")


async def test_edit_command_applies_corrections() -> None:
    mgr, bus, _ = _setup(confidence=0.3)
    await _run_command(mgr, "run", "doc_flow", '{"key": "a.pdf"}')
    await _drain(mgr, bus)
    run_id = (await mgr._control_plane.list_reviews())[0]["run_id"]

    reply = await _run_command(mgr, "edit", run_id, '{"classify": {"sender": "Globex"}}')
    assert "Edited" in reply
    await _drain(mgr, bus)
    assert json.loads(_texts(mgr)[-1])["saved"]["sender"] == "Globex"


async def test_answering_unknown_run_or_bad_json_is_explained() -> None:
    mgr, _, _ = _setup(confidence=0.3)
    assert "Unknown run" in await _run_command(mgr, "approve", "nope:1")
    assert "not valid JSON" in await _run_command(mgr, "edit", "nope:1", "{bad")


async def test_confident_run_completes_and_lists_in_runs() -> None:
    mgr, bus, _ = _setup(confidence=0.99)
    await _run_command(mgr, "run", "doc_flow", '{"key": "a.pdf"}')
    await _drain(mgr, bus)
    assert json.loads(_texts(mgr)[-1])["saved"]["sender"] == "ACME"
    runs = await _run_command(mgr, "runs")
    assert "[completed]" in runs and "(doc_flow)" in runs


async def test_workflow_tool_reports_pause_to_agent() -> None:
    from langclaw.workflows.bridge import make_workflow_tools

    registry = WorkflowRegistry()
    registry.register(graph_spec_of("doc_flow", DOC_FLOW))
    runtime = WorkflowRuntime(WorkflowsConfig(enabled=True))
    runtime.set_executor_factory(lambda _rt: FakeExecutor(confidence=0.2))

    (tool,) = make_workflow_tools(registry, runtime)
    text = await tool.ainvoke({"workflow_input": {"key": "a.pdf"}})
    assert "waiting for review" in text
    assert "/workflows approve doc_flow:" in text


def test_list_workflows_marks_file_graphs() -> None:
    mgr, _, _ = _setup(confidence=0.9)
    (wf,) = mgr._control_plane.list_workflows()
    assert (wf["source"], wf["editable"]) == ("file", True)


async def test_answer_review_feeds_ui_and_telegram_from_one_record() -> None:
    """Whatever surface answers, the pending list (the UI queue) updates at once."""
    mgr, bus, _ = _setup(confidence=0.3)
    await _run_command(mgr, "run", "doc_flow", '{"key": "a.pdf"}')
    await _drain(mgr, bus)
    (review,) = await mgr._control_plane.list_reviews()
    await mgr._control_plane.answer_review(
        review["run_id"], "approve", by="luca", via="ui", interrupt_id=review["interrupt_id"]
    )
    assert await mgr._control_plane.list_reviews() == []  # before the run even continues
    assert bus.published[0].channel == "telegram"  # result goes where the run started


async def test_a_run_started_from_chat_carries_the_users_role_into_its_steps() -> None:
    from langclaw.config.schema import PermissionsConfig, RoleConfig

    mgr, bus, ex = _setup(confidence=0.99)
    perms = PermissionsConfig(
        enabled=True,
        default_role="viewer",
        roles={"clerk": RoleConfig(tools=["bucket_read"], workflows=["doc_flow"])},
    )
    mgr._config.permissions = perms
    mgr._config.channels.telegram.user_roles = {"42": "clerk"}
    mgr._workflow_runtime.permissions = perms
    mgr._workflow_runtime.graph_runner.permissions = perms

    await _run_command(mgr, "run", "doc_flow", '{"key": "a.pdf"}')
    await _drain(mgr, bus)

    assert ex.tools_called() == ["bucket_read"]  # documents_insert was refused
    assert "may not use tool 'documents_insert'" in _texts(mgr)[-1]
    (record,) = await mgr._workflow_runtime.graph_runner.index.list(limit=5)
    assert (record["role"], record["status"]) == ("clerk", "failed")
