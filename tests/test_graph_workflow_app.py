"""App wiring for graph workflows: registration, file reconcile, durable run index."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from langclaw import Langclaw
from langclaw.config.schema import LangclawConfig
from tests.test_graph_workflows import DOC_FLOW, FakeExecutor


def _app(tmp_path: Path, *, interpreter: bool = False) -> Langclaw:
    cfg = LangclawConfig()
    cfg.agents.root_dir = str(tmp_path)
    cfg.workflows.enabled = True
    cfg.interpreter.enabled = interpreter
    cfg.checkpointer.sqlite.db_path = str(tmp_path / "state.db")
    return Langclaw(config=cfg)


def _write(app: Langclaw, name: str, body: dict | str) -> Path:
    directory = app._config.agents.workflows_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.graph.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


class S(TypedDict, total=False):
    n: int


def _builder() -> StateGraph:
    builder = StateGraph(S)
    builder.add_node("inc", lambda s: {"n": s["n"] + 1})
    builder.add_edge(START, "inc")
    builder.add_edge("inc", END)
    return builder


def test_app_workflow_registers_a_python_graph(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.workflow("counter", graph=_builder(), description="Add one")
    spec = app._workflows.get("counter")
    assert spec.source == "code"
    assert spec.description == "Add one"


def test_app_workflow_rejects_a_compiled_graph_clearly(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with pytest.raises(ValueError, match="uncompiled LangGraph StateGraph"):
        app.workflow("counter", graph=object())


def test_graph_files_load_without_the_interpreter(tmp_path: Path) -> None:
    app = _app(tmp_path, interpreter=False)
    _write(app, "doc_flow", DOC_FLOW)
    assert app._reload_workflow_files() is True
    spec = app._workflows.get("doc_flow")
    assert (spec.source, spec.description) == ("file", DOC_FLOW["description"])
    assert app._reload_workflow_files() is False  # idempotent


def test_graph_file_edit_and_delete_reconcile(tmp_path: Path) -> None:
    app = _app(tmp_path)
    path = _write(app, "doc_flow", DOC_FLOW)
    app._reload_workflow_files()
    old = app._workflows.get("doc_flow")

    _write(app, "doc_flow", {**DOC_FLOW, "description": "changed"})
    assert app._reload_workflow_files() is True
    assert app._workflows.get("doc_flow") is not old
    assert app._workflows.get("doc_flow").description == "changed"

    path.unlink()
    assert app._reload_workflow_files() is True
    assert app._workflows.get("doc_flow") is None


def test_invalid_graph_file_is_skipped_with_errors_kept(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _write(app, "broken", {"nodes": {"a": {"type": "llm", "prompt": "hi"}}, "edges": []})
    app._reload_workflow_files()
    assert app._workflows.get("broken") is None
    assert any("no edge from START" in e for e in app.graph_file_errors["broken"])


def test_graph_file_cannot_shadow_a_python_workflow(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.workflow("doc_flow", graph=_builder())
    _write(app, "doc_flow", DOC_FLOW)
    app._reload_workflow_files()
    assert app._workflows.get("doc_flow").graph_spec is None


async def test_runs_and_reviews_persist_across_app_restarts(tmp_path: Path) -> None:
    """Same SQLite files, new app: the paused run and its review are still there."""
    from langclaw.checkpointer import make_checkpointer_backend

    async def boot(app: Langclaw, stack: AsyncExitStack):
        cfg = app._config
        cp = make_checkpointer_backend("sqlite", db_path=cfg.checkpointer.sqlite.db_path, dsn="")
        await stack.enter_async_context(cp)
        await app._open_workflow_stores(stack, cfg.checkpointer, cfg)
        app._reload_workflow_files()
        app._attach_graph_runner(cfg, cp.get())
        runtime = app._workflow_runtime
        runtime.set_executor_factory(lambda _: FakeExecutor(confidence=0.2))
        return runtime

    first = _app(tmp_path)
    _write(first, "doc_flow", DOC_FLOW)
    async with AsyncExitStack() as stack:
        runtime = await boot(first, stack)
        spec = first._workflows.get("doc_flow")
        result = await runtime.run_graph(spec, {"key": "a.pdf"}, run_id="doc_flow:r1")
        assert result.status == "waiting"

    second = _app(tmp_path)
    async with AsyncExitStack() as stack:
        runtime = await boot(second, stack)
        runner = runtime.graph_runner
        (review,) = await runner.index.pending_reviews()
        assert review["run_id"] == "doc_flow:r1"
        claimed = await runner.claim_review("doc_flow:r1", {"action": "approve", "via": "ui"})
        done = await runtime.continue_graph_review(
            second._workflows.get("doc_flow"), "doc_flow:r1", claimed
        )
        assert done.status == "completed"
        assert done.output == {"saved": {"sender": "ACME", "confidence": 0.2}}
