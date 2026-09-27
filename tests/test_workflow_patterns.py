"""The workflow-pattern cookbook runs end to end (fake model, tools, and subagents)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from examples.workflow_patterns import PATTERNS, register_all
from examples.workflow_patterns._app import make_app
from langclaw.workflows import WorkflowRuntime
from langclaw.workflows.executor import StepRequest

EXAMPLES = Path(__file__).parent.parent / "examples" / "workflow_patterns"


class CookbookExecutor:
    """Deterministic stand-ins for the model, web_search, and the subagents."""

    def __init__(self) -> None:
        self.calls: list[StepRequest] = []
        self.hunt_round = 0

    async def __call__(self, req: StepRequest) -> Any:
        self.calls.append(req)
        if req.kind == "tool":
            return [{"title": "A result", "url": "https://example.com/r"}]
        if req.kind == "subagent":
            if req.target == "skeptic":
                return "VERDICT: supported\nWHY: docs say so\nSOURCE: https://example.com/d"
            return f"- notes on {req.payload[:40]}"
        schema = req.schema
        prompt = req.payload["prompt"]
        if schema is None:
            return f"text for: {prompt[:30]}"
        name = schema.__name__
        if name in ("Routing", "ClassifyOutput"):  # ClassifyOutput: triage.graph.json
            return schema(category="bug")
        if name == "Claims":
            return schema(claims=["claim one", "claim two"])
        if name == "Score":
            return schema(score=8 if "bold" in prompt or "TAGLINE" in prompt else 2, why="ok")
        if name == "Duel":
            return schema(winner="A", why="first is better")
        if name == "Cases":
            self.hunt_round += 1
            return schema(cases=[f"case {self.hunt_round}.{i}" for i in range(3)])
        raise AssertionError(f"unexpected schema {name}")


INPUTS: dict[str, dict] = {
    "triage": {"text": "login 500s with a + in the password"},
    "landscape": {"subject": "agent framework", "contenders": ["LangGraph", "CrewAI"]},
    "fact_check": {"question": "q?", "answer": "a.", "votes": 2},
    "tagline_studio": {"product": "workflows", "n": 3, "keep": 2},
    "prioritize": {"criterion": "impact", "items": ["A", "B", "C"]},
    "edge_hunt": {"target": "a date parser", "target_count": 5, "patience": 2},
}

EXPECT: dict[str, str] = {
    "triage": "# Triage — `bug`",
    "landscape": "# Agent Framework — landscape",
    "fact_check": "2/2 claims survived",
    "tagline_studio": "# Tagline studio — top 2 of 3",
    "prioritize": "🏆 **A**",
    "edge_hunt": "reached target of 5",
}


def _runtime() -> tuple[Any, WorkflowRuntime, CookbookExecutor]:
    app = register_all(make_app())
    runtime = WorkflowRuntime(app._config.workflows)
    ex = CookbookExecutor()
    runtime.set_executor_factory(lambda _: ex)
    return app, runtime, ex


@pytest.mark.parametrize("name", [wf for _, wf, _ in PATTERNS])
async def test_pattern_runs_end_to_end(name: str) -> None:
    app, runtime, _ = _runtime()
    spec = app._workflows.get(name)
    result = await runtime.run_graph(spec, INPUTS[name], run_id=f"{name}:t")
    assert result.status == "completed"
    assert isinstance(result.output, str)
    assert EXPECT[name] in result.output


async def test_fan_out_runs_one_scout_per_contender() -> None:
    app, runtime, ex = _runtime()
    await runtime.run_graph(app._workflows.get("landscape"), INPUTS["landscape"], run_id="l:1")
    scouts = [c for c in ex.calls if c.kind == "subagent"]
    assert sorted(c.payload.split("'")[1] for c in scouts) == ["CrewAI", "LangGraph"]


async def test_fact_check_asks_votes_skeptics_per_claim() -> None:
    app, runtime, ex = _runtime()
    await runtime.run_graph(app._workflows.get("fact_check"), INPUTS["fact_check"], run_id="f:1")
    assert len([c for c in ex.calls if c.target == "skeptic"]) == 4  # 2 claims x 2 votes


def test_triage_graph_file_is_valid_and_matches_the_docs() -> None:
    from langclaw.workflows.graph import parse_graph_spec

    raw = json.loads((EXAMPLES / "triage.graph.json").read_text())
    spec = parse_graph_spec("triage_file", raw, available_tools={"web_search"})
    assert [n.type for n in spec.nodes.values()] == ["llm", "branch", "tool", "llm"]


async def test_triage_graph_file_runs(tmp_path) -> None:
    from langclaw import Langclaw
    from langclaw.config.schema import LangclawConfig

    cfg = LangclawConfig()
    cfg.agents.root_dir = str(tmp_path)
    cfg.workflows.enabled = True
    app = Langclaw(config=cfg)
    folder = cfg.agents.workflows_dir
    folder.mkdir(parents=True)
    (folder / "triage_file.graph.json").write_text((EXAMPLES / "triage.graph.json").read_text())
    app._reload_workflow_files()

    runtime = WorkflowRuntime(cfg.workflows)
    ex = CookbookExecutor()
    runtime.set_executor_factory(lambda _: ex)
    spec = app._workflows.get("triage_file")
    result = await runtime.run_graph(spec, {"text": "how do I reset?"}, run_id="tf:1")
    assert result.status == "completed"
    assert result.output.startswith("text for: Category: bug")
    assert [c.target for c in ex.calls if c.kind == "tool"] == []  # bug → no web search


async def test_research_example_pauses_for_approval_then_delivers() -> None:
    import runpy

    app = runpy.run_path(str(EXAMPLES.parent / "workflow_research.py"), run_name="example")["app"]
    runtime = WorkflowRuntime(app._config.workflows)
    runtime.set_executor_factory(lambda _: CookbookExecutor())
    spec = app._workflows.get("research")

    paused = await runtime.run_graph(spec, {"topic": "batteries"}, run_id="research:1")
    assert paused.status == "waiting"
    assert paused.reviews[0]["editable"] == "draft"

    review = await runtime.graph_runner.claim_review(
        "research:1", {"action": "edit", "data": {"draft": "Edited brief"}}
    )
    done = await runtime.continue_graph_review(spec, "research:1", review)
    assert (done.status, done.output) == ("completed", "Edited brief")
