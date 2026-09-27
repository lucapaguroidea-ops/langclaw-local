"""Review requests: sent where the run started (+ a review chat), answered from
Telegram buttons, the UI, or commands — and every sent request shows the outcome."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from langclaw.config.schema import LangclawConfig, TelegramChannelConfig
from langclaw.gateway.base import BaseChannel
from langclaw.gateway.control import ConflictError, ControlPlane
from langclaw.gateway.reviews import (
    parse_review_callback,
    resolution_line,
    review_callback,
    review_request_text,
)
from langclaw.gateway.telegram import TelegramChannel
from langclaw.workflows import WorkflowRegistry, WorkflowRuntime
from tests.test_graph_workflows import DOC_FLOW, FakeExecutor, graph_spec_of

REQUEST = {
    "run_id": "doc_flow:abc",
    "workflow": "doc_flow",
    "key": "0123456789ab",
    "interrupt_id": "i" * 32,
    "message": "Sender ACME?",
    "data": {"classify": {"sender": "ACME"}},
    "editable": "classify",
}


# -- shared helpers -------------------------------------------------------------------


def test_callback_payload_round_trips_and_fits_telegram() -> None:
    for action in ("approve", "edit", "reject"):
        data = review_callback(action, REQUEST["key"])
        assert len(data.encode()) <= 64
        assert parse_review_callback(data) == (action, REQUEST["key"])
    assert parse_review_callback("other:a:x") is None
    assert parse_review_callback("wfr:z:x") is None


def test_request_text_shows_data_and_commands() -> None:
    text = review_request_text(REQUEST)
    assert "⏸ Review needed — doc_flow" in text and '"sender": "ACME"' in text
    assert "/workflows approve doc_flow:abc" in text
    assert "/workflows edit doc_flow:abc" in text
    assert "/workflows" not in review_request_text(REQUEST, commands=False)
    line = resolution_line({"action": "reject", "by": "@luca", "via": "ui"})
    assert line == "❌ Rejected by @luca via ui"


# -- routing and resolution ------------------------------------------------------------


class _Channel(BaseChannel):
    """Records review requests and resolutions; returns a message ref like Telegram."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[tuple[dict, dict]] = []
        self.resolved: list[tuple[dict, dict]] = []

    async def start(self, bus) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None

    async def send_ai_message(self, msg) -> None:  # pragma: no cover
        return None

    async def send_review_request(self, target, request):
        self.requests.append((target, request))
        return {"message_id": len(self.requests)}

    async def mark_review_resolved(self, notice, request, decision):
        self.resolved.append((notice, decision))


class _Bus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, msg) -> None:
        self.published.append(msg)


def _setup(*, review_chat: str = "") -> tuple[ControlPlane, WorkflowRuntime, dict[str, _Channel]]:
    config = LangclawConfig()
    if review_chat:
        config.workflows.review_channel = "telegram"
        config.workflows.review_chat_id = review_chat
    registry = WorkflowRegistry()
    registry.register(graph_spec_of("doc_flow", DOC_FLOW))
    runtime = WorkflowRuntime(config.workflows)
    runtime.set_executor_factory(lambda _: FakeExecutor(confidence=0.2))
    channels = {"telegram": _Channel("telegram"), "api": _Channel("api")}
    plane = ControlPlane(
        config=config,
        bus=_Bus(),
        channels=list(channels.values()),
        agent_names=["default"],
        workflow_registry=registry,
        workflow_runtime=runtime,
    )
    runtime.set_review_hook(plane.notify_review_requests)
    return plane, runtime, channels


def _origin(channel: str, chat: str) -> dict[str, str]:
    return {"channel": channel, "user_id": chat, "context_id": chat, "chat_id": chat}


async def _pause(runtime: WorkflowRuntime, reply_to: dict | None, run_id: str = "doc_flow:1"):
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    return await runtime.run_graph(spec, {"key": "a"}, run_id=run_id, reply_to=reply_to)


async def test_request_goes_where_the_run_started() -> None:
    _, runtime, channels = _setup()
    await _pause(runtime, _origin("telegram", "42"))
    ((target, request),) = channels["telegram"].requests
    assert target["chat_id"] == "42"
    assert request["message"] == "Sender ACME?" and request["editable"] == "classify"
    assert channels["api"].requests == []
    (review,) = (await runtime.graph_runner.index.get("doc_flow:1"))["reviews"]
    assert review["notices"] == [{"channel": "telegram", "chat_id": "42", "message_id": 1}]


async def test_ui_started_run_also_reaches_the_review_chat() -> None:
    _, runtime, channels = _setup(review_chat="42")
    await _pause(runtime, _origin("api", "turn-1"))
    assert len(channels["api"].requests) == 1
    assert channels["telegram"].requests[0][0]["chat_id"] == "42"


async def test_review_chat_is_not_notified_twice() -> None:
    _, runtime, channels = _setup(review_chat="42")
    await _pause(runtime, _origin("telegram", "42"))
    assert len(channels["telegram"].requests) == 1


async def test_cron_or_resume_run_without_origin_uses_the_review_chat() -> None:
    _, runtime, channels = _setup(review_chat="42")
    await _pause(runtime, None)
    assert len(channels["telegram"].requests) == 1


async def test_answering_in_the_ui_updates_the_telegram_message() -> None:
    plane, runtime, channels = _setup(review_chat="42")
    await _pause(runtime, _origin("api", "turn-1"))
    await plane.answer_review("doc_flow:1", "approve", by="luca", via="ui")
    notices = {n["channel"]: d for n, d in channels["telegram"].resolved}
    assert notices["telegram"]["via"] == "ui"
    assert len(channels["api"].resolved) == 1  # every sent request is updated


async def test_button_answer_by_key_and_first_answer_wins() -> None:
    plane, runtime, channels = _setup()
    await _pause(runtime, _origin("telegram", "42"))
    key = channels["telegram"].requests[0][1]["key"]
    review, run_id = await plane.answer_review_by_key(key, "reject", by="@luca", via="telegram")
    assert run_id == "doc_flow:1" and review["decision"]["action"] == "reject"
    with pytest.raises(ConflictError, match="already rejected by @luca via telegram"):
        await plane.answer_review_by_key(key, "approve", by="ui", via="ui")


async def test_a_failing_channel_does_not_block_the_run() -> None:
    _, runtime, channels = _setup()
    channels["telegram"].send_review_request = AsyncMock(side_effect=RuntimeError("down"))
    result = await _pause(runtime, _origin("telegram", "42"))
    assert result.status == "waiting"


# -- Telegram --------------------------------------------------------------------------


def _telegram(plane: Any = None) -> TelegramChannel:
    ch = TelegramChannel(TelegramChannelConfig(enabled=True, token="fake", allow_from=["42"]))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.edit_message_text = AsyncMock()
    ch._app = SimpleNamespace(bot=bot)
    if plane is not None:
        ch.set_control_plane(plane)
    return ch


async def test_telegram_sends_three_buttons() -> None:
    ch = _telegram()
    ref = await ch.send_review_request(_origin("telegram", "42"), REQUEST)
    assert ref == {"message_id": 7}
    kwargs = ch._app.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == "42"
    row = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in row] == ["✅ Approve", "✏️ Edit", "❌ Reject"]
    assert [parse_review_callback(b.callback_data)[0] for b in row] == ["approve", "edit", "reject"]


async def test_telegram_omits_edit_when_nothing_is_editable() -> None:
    ch = _telegram()
    await ch.send_review_request(_origin("telegram", "42"), {**REQUEST, "editable": ""})
    row = ch._app.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in row] == ["✅ Approve", "❌ Reject"]


async def test_telegram_resolution_edits_message_and_drops_buttons() -> None:
    ch = _telegram()
    await ch.mark_review_resolved(
        {"chat_id": "42", "message_id": 7},
        REQUEST,
        {"action": "approve", "by": "luca", "via": "ui"},
    )
    kwargs = ch._app.bot.edit_message_text.call_args.kwargs
    assert kwargs["message_id"] == 7 and kwargs["reply_markup"] is None
    assert kwargs["text"].endswith("✅ Approved by luca via ui")


def _button_press(data: str, user_id: int = 42, username: str = "luca"):
    from telegram import Update

    query = MagicMock()
    query.data = data
    query.from_user = SimpleNamespace(id=user_id, username=username, first_name="Luca")
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    update = MagicMock(spec=Update)
    update.callback_query = query
    return update, query


async def test_telegram_approve_button_answers_the_review() -> None:
    plane = MagicMock()
    plane.answer_review_by_key = AsyncMock(return_value=({}, "doc_flow:1"))
    ch = _telegram(plane)
    update, query = _button_press(review_callback("approve", "k1"))
    await ch._handle_review_button(update, None)
    plane.answer_review_by_key.assert_awaited_once_with("k1", "approve", by="@luca", via="telegram")
    query.answer.assert_awaited_once_with("Approved — continuing.")


async def test_telegram_late_button_press_shows_who_answered() -> None:
    plane = MagicMock()
    plane.answer_review_by_key = AsyncMock(
        side_effect=ConflictError("Run doc_flow:1 was already approved by luca via ui.")
    )
    ch = _telegram(plane)
    update, query = _button_press(review_callback("reject", "k1"))
    await ch._handle_review_button(update, None)
    query.answer.assert_awaited_once_with(
        "Run doc_flow:1 was already approved by luca via ui.", show_alert=True
    )


async def test_telegram_edit_button_explains_how_to_correct() -> None:
    plane = MagicMock()
    plane.review_request = AsyncMock(return_value=REQUEST)
    ch = _telegram(plane)
    update, query = _button_press(review_callback("edit", "k1"))
    await ch._handle_review_button(update, None)
    text = query.message.reply_text.call_args.args[0]
    assert '/workflows edit doc_flow:abc {"classify": {"sender": "ACME"}}' in text


async def test_telegram_rejects_buttons_from_strangers() -> None:
    plane = MagicMock()
    plane.answer_review_by_key = AsyncMock()
    ch = _telegram(plane)
    update, query = _button_press(review_callback("approve", "k1"), user_id=99, username="x")
    await ch._handle_review_button(update, None)
    plane.answer_review_by_key.assert_not_awaited()
    assert query.answer.call_args.kwargs == {"show_alert": True}


# -- end to end: Telegram button → run continues → other surfaces updated ---------------


async def test_telegram_button_end_to_end() -> None:
    plane, runtime, channels = _setup(review_chat="42")
    telegram = _telegram(plane)
    plane._channels = [telegram, channels["api"]]
    await _pause(runtime, _origin("api", "turn-1"))

    sent = telegram._app.bot.send_message.call_args.kwargs
    approve = sent["reply_markup"].inline_keyboard[0][0].callback_data
    update, query = _button_press(approve)
    await telegram._handle_review_button(update, None)

    assert await plane.list_reviews() == []  # the UI queue is empty at once
    edit = telegram._app.bot.edit_message_text.call_args.kwargs
    assert edit["text"].endswith("✅ Approved by @luca via telegram")
    assert channels["api"].resolved[0][1]["via"] == "telegram"
    published = plane._bus.published[-1]
    assert published.channel == "api" and published.metadata["review"]["decision"]


def test_agent_tool_runs_reply_to_the_chat_that_called_them() -> None:
    from langclaw.context import LangclawContext
    from langclaw.workflows.bridge import _origin as tool_origin

    ctx = LangclawContext(channel="telegram", user_id="42", context_id="42", chat_id="42")
    assert tool_origin(SimpleNamespace(context=ctx)) == _origin("telegram", "42")
    assert tool_origin(None) is None
    assert tool_origin(SimpleNamespace(context=LangclawContext())) is None
