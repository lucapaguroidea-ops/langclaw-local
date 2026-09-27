"""Editing workflows: the shared file service, the agent tool, and the API endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from langclaw import Langclaw
from langclaw.config.schema import LangclawConfig
from langclaw.workflows.files import WorkflowFileNotFound, WorkflowFiles
from langclaw.workflows.graph import GraphSpecError
from tests.test_graph_workflows import DOC_FLOW, FakeExecutor
from tests.test_workflows import _graph

CATALOG = {"tools": ["bucket_read", "documents_insert", "web_search"], "subagents": ["scout"]}


def _app(tmp_path: Path) -> Langclaw:
    cfg = LangclawConfig()
    cfg.agents.root_dir = str(tmp_path)
    cfg.workflows.enabled = True
    cfg.checkpointer.sqlite.db_path = str(tmp_path / "state.db")
    return Langclaw(config=cfg)


def _files(app: Langclaw) -> WorkflowFiles:
    runtime = app._get_workflow_runtime(app._config)
    runtime.set_catalog(**CATALOG)
    return runtime.files


def _edited(description: str) -> dict:
    return {**DOC_FLOW, "description": description}


# -- WorkflowFiles -----------------------------------------------------------------


def test_save_creates_and_goes_live(tmp_path: Path) -> None:
    app = _app(tmp_path)
    files = _files(app)
    result = files.save("doc_flow", DOC_FLOW)
    assert result == {"name": "doc_flow", "created": True, "warnings": []}
    assert app._workflows.get("doc_flow").source == "file"
    assert files.names() == ["doc_flow"]
    assert files.versions("doc_flow") == []  # nothing to snapshot yet


def test_updates_are_versioned_and_restorable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    files = _files(app)
    files.save("doc_flow", _edited("v1"))
    files.save("doc_flow", _edited("v2"))
    files.save("doc_flow", _edited("v3"))
    versions = files.versions("doc_flow")
    assert len(versions) == 2 and versions[0]["saved_at"]
    assert files.read_version("doc_flow", versions[0]["version"])["description"] == "v2"
    assert files.read_version("doc_flow", versions[1]["version"])["description"] == "v1"

    files.restore("doc_flow", versions[1]["version"])
    assert app._workflows.get("doc_flow").description == "v1"
    assert len(files.versions("doc_flow")) == 3  # v3 was snapshotted before restoring


def test_delete_keeps_history_and_history_is_not_loaded(tmp_path: Path) -> None:
    app = _app(tmp_path)
    files = _files(app)
    files.save("doc_flow", DOC_FLOW)
    files.delete("doc_flow")
    assert app._workflows.get("doc_flow") is None
    (version,) = files.versions("doc_flow")
    files.restore("doc_flow", version["version"])
    assert app._workflows.get("doc_flow") is not None
    app._reload_workflow_files()
    assert app._workflows.names() == ["doc_flow"]  # .history/ never loads


def test_invalid_graph_is_rejected_with_every_error(tmp_path: Path) -> None:
    files = _files(_app(tmp_path))
    bad = {"nodes": {"a": {"type": "llm", "prompt": "{{typo}}"}}, "edges": []}
    check = files.validate("bad", bad)
    assert check["valid"] is False and len(check["errors"]) >= 2
    with pytest.raises(GraphSpecError):
        files.save("bad", bad)
    assert not files.exists("bad")


def test_unknown_tools_are_warnings_not_errors(tmp_path: Path) -> None:
    files = _files(_app(tmp_path))
    raw = json.loads(json.dumps(DOC_FLOW))
    raw["nodes"]["save"]["tool"] = "no_such_tool"
    check = files.validate("doc_flow", raw)
    assert check["valid"] is True
    assert check["warnings"] == ["node 'save': tool 'no_such_tool' is not available right now"]
    assert files.save("doc_flow", raw)["warnings"] == check["warnings"]


def test_code_workflows_are_protected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.workflow("echo", graph=_graph())
    files = _files(app)
    with pytest.raises(ValueError, match="defined in code"):
        files.save("echo", DOC_FLOW)
    with pytest.raises(ValueError, match="defined in code"):
        files.delete("echo")


def test_unknown_names_and_versions(tmp_path: Path) -> None:
    files = _files(_app(tmp_path))
    with pytest.raises(WorkflowFileNotFound):
        files.read("nope")
    with pytest.raises(WorkflowFileNotFound):
        files.delete("nope")
    with pytest.raises(WorkflowFileNotFound):
        files.read_version("nope", "../../etc")


# -- manage_workflows tool ------------------------------------------------------------


def _tool(app: Langclaw):
    from langclaw.workflows.bridge import make_manage_workflows_tool

    return make_manage_workflows_tool(_files(app))


def test_tool_full_lifecycle(tmp_path: Path) -> None:
    app = _app(tmp_path)
    tool = _tool(app)
    assert "human_review" in tool.invoke({"action": "format"})["format"]
    assert tool.invoke({"action": "list"}) == {"workflows": []}

    bad = tool.invoke({"action": "save", "name": "doc_flow", "graph": {"nodes": {}}})
    assert bad["error"] == "The workflow is invalid." and bad["errors"]

    saved = tool.invoke({"action": "save", "name": "doc_flow", "graph": json.dumps(DOC_FLOW)})
    assert saved["created"] is True
    assert app._workflows.get("doc_flow") is not None

    got = tool.invoke({"action": "get", "name": "doc_flow"})
    assert got["graph"]["nodes"]["classify"]["type"] == "llm"

    tool.invoke({"action": "save", "name": "doc_flow", "graph": _edited("v2")})
    (version,) = tool.invoke({"action": "versions", "name": "doc_flow"})["versions"]
    tool.invoke({"action": "restore", "name": "doc_flow", "version": version["version"]})
    assert app._workflows.get("doc_flow").description == DOC_FLOW["description"]

    assert tool.invoke({"action": "delete", "name": "doc_flow"})["deleted"] == "doc_flow"
    assert "No workflow file" in tool.invoke({"action": "get", "name": "doc_flow"})["error"]
    assert "Unknown action" in tool.invoke({"action": "explode", "name": "x"})["error"]


def test_tool_refuses_code_workflows_as_an_error_dict(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.workflow("echo", graph=_graph())
    out = _tool(app).invoke({"action": "save", "name": "echo", "graph": DOC_FLOW})
    assert "defined in code" in out["error"]


def test_builder_adds_the_tool_and_authoring_prompt(tmp_path: Path, monkeypatch) -> None:
    import deepagents

    captured: dict = {}
    monkeypatch.setattr(deepagents, "create_deep_agent", lambda **kw: captured.update(kw))
    app = _app(tmp_path)
    app.create_agent(model=object())
    names = [t.name for t in captured["tools"]]
    assert "manage_workflows" in names
    assert "`manage_workflows`" in captured["system_prompt"]
    catalog = app._workflow_runtime.catalog()
    assert "manage_workflows" not in catalog["tools"]  # steps can't edit workflows


# -- API ------------------------------------------------------------------------------

aiohttp = pytest.importorskip("aiohttp")


class _Bus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, msg) -> None:
        self.published.append(msg)


@pytest.fixture
async def api(tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from langclaw.config.schema import ApiChannelConfig
    from langclaw.gateway.api import ApiChannel
    from langclaw.gateway.control import ControlPlane

    app = _app(tmp_path)
    runtime = app._get_workflow_runtime(app._config)
    runtime.set_catalog(**CATALOG)
    runtime.set_executor_factory(lambda _: FakeExecutor(confidence=0.3))
    channel = ApiChannel(ApiChannelConfig(enabled=True, token="t" * 20, user_id="admin"))
    bus = _Bus()
    channel._bus = bus
    plane = ControlPlane(
        config=app._config,
        bus=bus,
        channels=[channel],
        agent_names=["default"],
        workflow_registry=app._workflows,
        workflow_runtime=runtime,
        workflow_file_errors=lambda: app.graph_file_errors,
    )
    channel.set_control_plane(plane)
    client = TestClient(TestServer(channel.build_app()))
    await client.start_server()
    try:
        yield app, runtime, bus, client
    finally:
        await client.close()


AUTH = {"Authorization": "Bearer " + "t" * 20}


async def test_api_edit_validate_version_restore(api) -> None:
    app, _, _, client = api
    resp = await client.post("/v1/workflows/doc_flow/validate", json={"nodes": {}}, headers=AUTH)
    assert resp.status == 200 and (await resp.json())["valid"] is False

    resp = await client.put("/v1/workflows/doc_flow", json=DOC_FLOW, headers=AUTH)
    body = await resp.json()
    assert resp.status == 200 and body["created"] is True and "mermaid" in body

    await client.put("/v1/workflows/doc_flow", json=_edited("v2"), headers=AUTH)
    versions = (await (await client.get("/v1/workflows/doc_flow/versions", headers=AUTH)).json())[
        "versions"
    ]
    assert len(versions) == 1
    v = versions[0]["version"]
    old = await (await client.get(f"/v1/workflows/doc_flow/versions/{v}", headers=AUTH)).json()
    assert old["graph"]["description"] == DOC_FLOW["description"]
    resp = await client.post(f"/v1/workflows/doc_flow/versions/{v}/restore", headers=AUTH)
    assert (await resp.json())["description"] == DOC_FLOW["description"]

    catalog = await (await client.get("/v1/catalog", headers=AUTH)).json()
    assert catalog == CATALOG


async def test_api_lists_invalid_files_so_they_can_be_fixed(api) -> None:
    app, _, _, client = api
    folder = app._config.agents.workflows_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "broken.graph.json").write_text('{"nodes": {}}')
    app._reload_workflow_files()
    listed = (await (await client.get("/v1/workflows", headers=AUTH)).json())["workflows"]
    (broken,) = [w for w in listed if w["name"] == "broken"]
    assert broken["valid"] is False and broken["errors"]
    detail = await (await client.get("/v1/workflows/broken", headers=AUTH)).json()
    assert detail["graph"] == {"nodes": {}}


async def test_api_review_queue_answer_and_conflict(api) -> None:
    app, runtime, bus, client = api
    await client.put("/v1/workflows/doc_flow", json=DOC_FLOW, headers=AUTH)
    spec = app._workflows.get("doc_flow")
    reply_to = {"channel": "telegram", "user_id": "42", "context_id": "c", "chat_id": "42"}
    await runtime.run_graph(spec, {"key": "a.pdf"}, run_id="doc_flow:r1", reply_to=reply_to)

    reviews = (await (await client.get("/v1/reviews", headers=AUTH)).json())["reviews"]
    assert [r["run_id"] for r in reviews] == ["doc_flow:r1"]

    runs = await (await client.get("/v1/workflows/doc_flow/runs", headers=AUTH)).json()
    assert runs["runs"][0]["status"] == "waiting"
    assert runs["runs"][0]["pending_reviews"] == 1

    resp = await client.post(
        "/v1/runs/doc_flow:r1/review",
        json={"action": "approve", "by": "luca", "via": "ui"},
        headers=AUTH,
    )
    assert resp.status == 200
    assert bus.published[-1].channel == "telegram"  # continues where the run started
    assert bus.published[-1].metadata["review"]["decision"]["via"] == "ui"

    resp = await client.post("/v1/runs/doc_flow:r1/review", json={"action": "reject"}, headers=AUTH)
    body = await resp.json()
    assert resp.status == 409
    assert "already approved by luca via ui" in body["error"]
    assert body["decision"]["by"] == "luca"

    run = await (await client.get("/v1/runs/doc_flow:r1", headers=AUTH)).json()
    assert run["reviews"][0]["decision"]["action"] == "approve"
    assert [s["node"] for s in run["steps"]][:2] == ["fetch", "classify"]


async def test_api_review_input_errors(api) -> None:
    _, _, _, client = api
    resp = await client.post("/v1/runs/x:1/review", json={}, headers=AUTH)
    assert resp.status == 400
    resp = await client.post("/v1/runs/x:1/review", json={"action": "approve"}, headers=AUTH)
    assert resp.status == 404
    resp = await client.get("/v1/workflows/nope/runs", headers=AUTH)
    assert resp.status == 404
