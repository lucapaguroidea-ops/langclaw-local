"""Conversations survive restarts; /reset truly clears; history is readable."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from langclaw.config.schema import LangclawConfig
from langclaw.gateway.control import ControlPlane
from langclaw.session import SessionManager


async def test_thread_ids_are_stable_across_restarts() -> None:
    first = await SessionManager().get_or_create_thread("telegram", "u1", "default")
    after_restart = await SessionManager().get_or_create_thread("telegram", "u1", "default")
    other = await SessionManager().get_or_create_thread("telegram", "u2", "default")

    assert first == after_restart
    assert first != other


async def test_reset_deletes_the_threads_checkpoints() -> None:
    checkpointer = AsyncMock()
    sm = SessionManager(checkpointer=checkpointer)
    thread_id = await sm.get_or_create_thread("telegram", "u1", "default")

    assert await sm.delete_thread("telegram", "u1", "default") is True
    checkpointer.adelete_thread.assert_awaited_once_with(thread_id)


def _echo_graph(checkpointer):
    def reply(state: MessagesState):
        return {"messages": [AIMessage(content=f"echo: {state['messages'][-1].content}")]}

    g = StateGraph(MessagesState)
    g.add_node("reply", reply)
    g.add_edge(START, "reply")
    g.add_edge("reply", END)
    return g.compile(checkpointer=checkpointer)


async def test_history_reads_messages_from_checkpointer() -> None:
    saver = InMemorySaver()
    sessions = SessionManager(checkpointer=saver)
    config = await sessions.get_config("api", "admin", "web")
    await _echo_graph(saver).ainvoke({"messages": [HumanMessage(content="hi")]}, config)

    plane = ControlPlane(
        config=LangclawConfig(),
        bus=AsyncMock(),
        channels=[],
        agent_names=["default"],
        sessions=sessions,
        checkpointer=saver,
    )

    assert await plane.history("api", "admin", "web") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "echo: hi"},
    ]
    assert await plane.history("api", "admin", "empty") == []


async def test_history_without_checkpointer_is_feature_disabled() -> None:
    from langclaw.gateway.control import FeatureDisabledError

    plane = ControlPlane(config=LangclawConfig(), bus=AsyncMock(), channels=[], agent_names=[])
    with pytest.raises(FeatureDisabledError):
        await plane.history("api", "admin", "web")
