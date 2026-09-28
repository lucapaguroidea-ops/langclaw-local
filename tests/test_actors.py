"""Who is acting comes from the channel, not from the model."""

from __future__ import annotations

import asyncio

from langclaw.actors import actor_id, actor_scope, current_actor


def test_actor_scope_nests_and_resets() -> None:
    assert current_actor() == ""
    with actor_scope("telegram:1"):
        assert current_actor() == "telegram:1"
        with actor_scope("api:ana"):
            assert current_actor() == "api:ana"
        assert current_actor() == "telegram:1"
    assert current_actor() == ""
    assert actor_id("telegram", "42") == "telegram:42" and actor_id("", "42") == ""


async def test_tasks_started_in_a_scope_keep_its_actor() -> None:
    async def who() -> str:
        await asyncio.sleep(0)
        return current_actor()

    with actor_scope("telegram:7"):
        task = asyncio.create_task(who())
    assert await task == "telegram:7"


async def test_a_chat_turn_acts_for_its_sender_and_workflow_messages_do_not() -> None:
    from langclaw.bus.base import InboundMessage
    from langclaw.gateway.manager import GatewayManager

    gateway = GatewayManager.__new__(GatewayManager)
    gateway._tenants = None
    seen: list[str] = []

    async def handle(msg) -> None:
        seen.append(current_actor())

    gateway._handle_message = handle
    chat = InboundMessage(channel="telegram", user_id="42", context_id="c", content="hi")
    await gateway._handle(chat)
    await gateway._handle(InboundMessage(**{**chat.__dict__, "origin": "workflow"}))
    assert seen == ["telegram:42", ""]
