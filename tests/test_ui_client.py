"""ui/client.py — the Streamlit UI's typed client for the langclaw control-plane API."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "langclaw_ui_client", Path(__file__).parents[1] / "ui" / "client.py"
)
client_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client_mod)
LangclawClient = client_mod.LangclawClient
LangclawError = client_mod.LangclawError


def _client(handler) -> tuple[LangclawClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    c = LangclawClient("http://lc:18790", "tok", transport=httpx.MockTransport(record))
    return c, seen


def test_sends_bearer_token_and_parses_json() -> None:
    c, seen = _client(lambda r: httpx.Response(200, json={"channels": ["api"]}))
    assert c.status() == {"channels": ["api"]}
    assert seen[0].headers["Authorization"] == "Bearer tok"
    assert str(seen[0].url) == "http://lc:18790/v1/status"


def test_errors_raise_with_server_message() -> None:
    c, _ = _client(lambda r: httpx.Response(409, json={"error": "Workflows are disabled."}))
    with pytest.raises(LangclawError, match="Workflows are disabled") as exc:
        c.workflows()
    assert exc.value.status == 409


def test_chat_waits_until_turn_done() -> None:
    responses = iter(
        [
            httpx.Response(202, json={"turn_id": "t1", "status": "running", "messages": []}),
            httpx.Response(
                200,
                json={
                    "turn_id": "t1",
                    "status": "done",
                    "messages": [{"type": "ai", "content": "hi"}],
                },
            ),
        ]
    )
    c, seen = _client(lambda r: next(responses))

    turn = c.chat_and_wait("hello", context_id="ui")

    assert turn["status"] == "done"
    assert json.loads(seen[0].content) == {"content": "hello", "context_id": "ui"}
    assert seen[1].url.path == "/v1/turns/t1"


def test_run_workflow_serialises_input_and_follows_turn() -> None:
    responses = iter(
        [
            httpx.Response(202, json={"run_id": "echo:1", "turn_id": "t9"}),
            httpx.Response(200, json={"turn_id": "t9", "status": "done", "messages": []}),
        ]
    )
    c, seen = _client(lambda r: next(responses))

    turn = c.run_workflow_and_wait("echo", {"q": 1})

    assert json.loads(seen[0].content) == {"input": {"q": 1}}
    assert turn["turn_id"] == "t9"


def test_unreachable_server_raises_langclaw_error() -> None:
    def boom(request):
        raise httpx.ConnectError("refused")

    c, _ = _client(boom)
    with pytest.raises(LangclawError, match="Cannot reach langclaw"):
        c.status()
