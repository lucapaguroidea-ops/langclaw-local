"""ControlPlane: the structured management surface behind /workflows and the HTTP API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from langclaw.config.schema import LangclawConfig
from langclaw.gateway.control import (
    ControlPlane,
    FeatureDisabledError,
    NotFoundError,
)
from langclaw.workflows import WorkflowRegistry, WorkflowSpec
from tests.test_graph_workflows import DOC_FLOW
from tests.test_workflows import _graph


def _registry() -> WorkflowRegistry:
    reg = WorkflowRegistry()
    reg.register(WorkflowSpec(name="echo", description="echo input", graph=_graph()))
    return reg


def _app_plane(tmp_path):
    """A plane over a real app's registry + file reconcile (save → live)."""
    from langclaw import Langclaw

    cfg = LangclawConfig()
    cfg.agents.root_dir = str(tmp_path)
    cfg.workflows.enabled = True
    app = Langclaw(config=cfg)
    app.workflow("echo", graph=_graph(), description="echo input")
    plane = _plane(
        workflow_registry=app._workflows,
        workflows_dir=cfg.agents.workflows_dir,
        workflows_reload_cb=app._reload_workflow_files,
        workflow_file_errors=lambda: app.graph_file_errors,
    )
    return app, plane


class _Channel:
    def __init__(self, name: str) -> None:
        self.name = name


def _plane(**overrides) -> ControlPlane:
    kwargs = {
        "config": LangclawConfig(),
        "bus": AsyncMock(),
        "channels": [_Channel("telegram"), _Channel("api")],
        "agent_names": ["default"],
    }
    kwargs.update(overrides)
    return ControlPlane(**kwargs)


# --- status -------------------------------------------------------------------


def test_status_reports_channels_agents_and_features() -> None:
    status = _plane(workflow_registry=_registry()).status()

    assert status["channels"] == ["telegram", "api"]
    assert status["agents"] == ["default"]
    assert status["features"]["workflows"] is True
    assert status["features"]["schedules"] is False
    assert "version" in status and "model" in status


# --- workflows ----------------------------------------------------------------


def test_workflows_disabled_raises_feature_disabled() -> None:
    with pytest.raises(FeatureDisabledError):
        _plane().list_workflows()


def test_list_and_get_workflow() -> None:
    plane = _plane(workflow_registry=_registry())

    assert plane.list_workflows() == [
        {"name": "echo", "description": "echo input", "source": "code", "editable": False}
    ]
    assert plane.get_workflow("echo")["name"] == "echo"
    with pytest.raises(NotFoundError):
        plane.get_workflow("missing")


def test_save_workflow_requires_a_workflows_folder() -> None:
    plane = _plane(workflow_registry=_registry())
    with pytest.raises(FeatureDisabledError, match="folder"):
        plane.save_workflow("doc_flow", DOC_FLOW)


def test_save_get_and_delete_a_workflow_file(tmp_path) -> None:
    app, plane = _app_plane(tmp_path)
    saved = plane.save_workflow("doc_flow", DOC_FLOW)
    assert saved["source"] == "file" and saved["editable"] is True
    assert saved["graph"]["nodes"]["classify"]["type"] == "llm"
    assert "classify" in saved["mermaid"]
    path = app._config.agents.workflows_dir / "doc_flow.graph.json"
    assert path.exists()
    assert "doc_flow" in [w["name"] for w in plane.list_workflows()]

    assert plane.delete_workflow("doc_flow") is True
    assert not path.exists()
    assert app._workflows.get("doc_flow") is None
    with pytest.raises(NotFoundError):
        plane.delete_workflow("doc_flow")


def test_save_rejects_an_invalid_graph_listing_every_problem(tmp_path) -> None:
    _, plane = _app_plane(tmp_path)
    bad = {"nodes": {"a": {"type": "llm", "prompt": "{{nope}}"}}, "edges": []}
    with pytest.raises(ValueError) as exc:
        plane.save_workflow("bad", bad)
    assert "no edge from START" in str(exc.value)
    assert "unknown key 'nope'" in str(exc.value)
    assert plane.validate_workflow("bad", bad)["valid"] is False
    assert plane.validate_workflow("doc_flow", DOC_FLOW) == {"valid": True, "errors": []}


def test_save_rejects_bad_name_and_code_workflow(tmp_path) -> None:
    _, plane = _app_plane(tmp_path)
    with pytest.raises(ValueError, match="snake_case"):
        plane.save_workflow("bad-name", DOC_FLOW)
    with pytest.raises(ValueError, match="defined in code"):
        plane.save_workflow("echo", DOC_FLOW)


def test_workflow_file_errors_are_reported(tmp_path) -> None:
    app, plane = _app_plane(tmp_path)
    folder = app._config.agents.workflows_dir
    folder.mkdir(parents=True)
    (folder / "broken.graph.json").write_text("{nope")
    app._reload_workflow_files()
    assert "not valid JSON" in plane.workflow_file_errors()["broken"][0]


async def test_start_workflow_publishes_origin_workflow_message() -> None:
    bus = AsyncMock()
    plane = _plane(bus=bus, workflow_registry=_registry())

    run_id = await plane.start_workflow(
        "echo", '{"q": 1}', channel="api", user_id="admin", context_id="ui", chat_id="t1"
    )

    assert run_id.startswith("echo:")
    published = bus.publish.call_args[0][0]
    assert published.origin == "workflow"
    assert published.chat_id == "t1"
    assert published.metadata == {
        "workflow_name": "echo",
        "workflow_input": '{"q": 1}',
        "run_id": run_id,
    }


async def test_start_unknown_workflow_raises() -> None:
    plane = _plane(workflow_registry=_registry())
    with pytest.raises(NotFoundError):
        await plane.start_workflow("nope", "", channel="api", user_id="a", context_id="c")


async def test_runs_without_runtime() -> None:
    runs = await _plane(workflow_registry=_registry()).list_runs()
    assert runs == {"runs": []}


def test_cancel_run() -> None:
    task = MagicMock()
    plane = _plane(workflow_registry=_registry(), live_runs={"echo:1": task})

    assert plane.cancel_run("echo:1") is True
    task.cancel.assert_called_once()
    with pytest.raises(NotFoundError):
        plane.cancel_run("echo:2")


# --- schedules ----------------------------------------------------------------


def test_schedules_disabled_raises_feature_disabled() -> None:
    with pytest.raises(FeatureDisabledError, match="CRON__ENABLED"):
        _plane().require_schedules()


async def test_add_list_remove_schedule() -> None:
    from langclaw.cron.scheduler import CronJob

    cron = MagicMock()
    cron.add_job = AsyncMock(return_value="job-1")
    cron.list_jobs = AsyncMock(
        return_value=[
            CronJob(
                id="job-1",
                name="morning",
                message="summarise news",
                channel="telegram",
                user_id="42",
                context_id="default",
                chat_id="42",
                schedule="0 9 * * *",
            )
        ]
    )
    cron.remove_job = AsyncMock(side_effect=[True, False])
    plane = _plane(cron_manager=cron)

    job_id = await plane.add_schedule(
        name="morning",
        channel="telegram",
        user_id="42",
        message="summarise news",
        cron_expr="0 9 * * *",
    )
    assert job_id == "job-1"
    assert cron.add_job.call_args.kwargs["chat_id"] == "42"  # defaults to user_id

    jobs = await plane.list_schedules()
    assert jobs[0]["id"] == "job-1" and jobs[0]["schedule"] == "0 9 * * *"

    assert await plane.remove_schedule("job-1") is True
    with pytest.raises(NotFoundError):
        await plane.remove_schedule("job-1")


async def test_add_schedule_validates_channel_and_target() -> None:
    cron = MagicMock()
    cron.add_job = AsyncMock(return_value="x")
    plane = _plane(cron_manager=cron)

    with pytest.raises(ValueError, match="channel"):
        await plane.add_schedule(
            name="n", channel="slack", user_id="1", message="m", cron_expr="* * * * *"
        )
    with pytest.raises(ValueError, match="message or workflow_name"):
        await plane.add_schedule(name="n", channel="telegram", user_id="1", cron_expr="* * * * *")
    with pytest.raises(ValueError, match="cron_expr or every_seconds"):
        await plane.add_schedule(name="n", channel="telegram", user_id="1", message="m")


def test_status_reports_mcp_servers() -> None:
    report = [{"name": "docs", "transport": "sse", "tools": ["mcp_docs_search"], "error": None}]
    status = _plane(mcp_servers=report).status()
    assert status["features"]["mcp"] is True
    assert status["mcp_servers"] == report
