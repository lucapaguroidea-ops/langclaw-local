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


def test_start_run_then_follow_turn_until_done() -> None:
    responses = iter(
        [
            httpx.Response(202, json={"run_id": "echo:1", "turn_id": "t9"}),
            httpx.Response(200, json={"turn_id": "t9", "status": "running", "messages": []}),
            httpx.Response(200, json={"turn_id": "t9", "status": "done", "messages": []}),
        ]
    )
    c, seen = _client(lambda r: next(responses))

    started = c.start_run("echo", {"q": 1})
    turn = c.follow_turn(started["turn_id"])

    assert json.loads(seen[0].content) == {"input": {"q": 1}}
    assert seen[1].url.path == "/v1/turns/t9"
    assert turn["status"] == "done"


def test_runs_route_by_workflow_and_filter_status() -> None:
    c, seen = _client(lambda r: httpx.Response(200, json={"runs": []}))
    c.runs("doc_flow", status="waiting")
    c.runs()
    assert seen[0].url.path == "/v1/workflows/doc_flow/runs"
    assert seen[0].url.params["status"] == "waiting"
    assert seen[1].url.path == "/v1/runs"


def test_answer_review_sends_decision_and_surfaces_conflicts() -> None:
    c, seen = _client(
        lambda r: httpx.Response(
            409, json={"error": "Run r:1 was already approved by luca via telegram."}
        )
    )
    with pytest.raises(LangclawError, match="already approved") as exc:
        c.answer_review("r:1", "edit", interrupt_id="i1", data={"x": 1}, by="web")
    assert exc.value.status == 409
    assert seen[0].url.path == "/v1/runs/r:1/review"
    assert json.loads(seen[0].content) == {
        "action": "edit",
        "interrupt_id": "i1",
        "by": "web",
        "via": "ui",
        "data": {"x": 1},
    }


def test_workflow_editing_calls() -> None:
    c, seen = _client(lambda r: httpx.Response(200, json={"versions": [], "valid": True}))
    c.validate_workflow("doc_flow", {"nodes": {}})
    c.versions("doc_flow")
    c.restore("doc_flow", "v1")
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/workflows/doc_flow/validate"),
        ("GET", "/v1/workflows/doc_flow/versions"),
        ("POST", "/v1/workflows/doc_flow/versions/v1/restore"),
    ]


def test_unreachable_server_raises_langclaw_error() -> None:
    def boom(request):
        raise httpx.ConnectError("refused")

    c, _ = _client(boom)
    with pytest.raises(LangclawError, match="Cannot reach langclaw"):
        c.status()


def test_whoami_names_the_person_behind_a_token() -> None:
    c, seen = _client(lambda r: httpx.Response(200, json={"person": "ana"}))
    assert c.whoami() == {"person": "ana"}
    assert seen[0].url.path == "/v1/whoami"
