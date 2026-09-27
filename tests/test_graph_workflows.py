"""Graph workflows: file format, compilation, runs, reviews, and crash resume."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from langclaw.workflows.executor import StepRequest
from langclaw.workflows.graph import (
    GraphSpecError,
    GraphWorkflowRunner,
    ReviewAlreadyResolved,
    RunIndex,
    build_state_graph,
    load_graph_files,
    parse_graph_spec,
    request_review,
    steps,
)
from langclaw.workflows.registry import WorkflowSpec

DOC_FLOW: dict[str, Any] = {
    "description": "Classify a document and file it.",
    "input": {"key": {"type": "string"}},
    "nodes": {
        "fetch": {"type": "tool", "tool": "bucket_read", "args": {"key": "{{input.key}}"}},
        "classify": {
            "type": "llm",
            "prompt": "Who sent this?\n{{fetch}}",
            "output": {"sender": {"type": "string"}, "confidence": {"type": "number"}},
        },
        "check": {
            "type": "branch",
            "rules": [
                {"if": {"path": "classify.confidence", "op": "lt", "value": 0.8}, "then": "review"}
            ],
            "else": "save",
        },
        "review": {
            "type": "human_review",
            "message": "Sender {{classify.sender}}?",
            "show": ["classify"],
            "editable": "classify",
        },
        "save": {"type": "tool", "tool": "documents_insert", "args": {"meta": "{{classify}}"}},
    },
    "edges": [
        {"from": "START", "to": "fetch"},
        {"from": "fetch", "to": "classify"},
        {"from": "classify", "to": "check"},
        {"from": "review", "to": "save"},
    ],
    "output": "save",
}


class FakeExecutor:
    """Canned tools/LLM; records every request."""

    def __init__(self, confidence: float = 0.95) -> None:
        self.confidence = confidence
        self.calls: list[StepRequest] = []

    async def __call__(self, request: StepRequest) -> Any:
        self.calls.append(request)
        if request.kind == "tool" and request.target == "bucket_read":
            return f"text of {request.payload['key']}"
        if request.kind == "tool" and request.target == "documents_insert":
            return {"saved": request.payload["meta"]}
        if request.kind == "llm":
            return request.schema(sender="ACME", confidence=self.confidence)
        raise AssertionError(f"unexpected {request}")

    def tools_called(self) -> list[str]:
        return [c.target for c in self.calls if c.kind == "tool"]


def graph_spec_of(name: str, raw: dict[str, Any]) -> WorkflowSpec:
    spec = parse_graph_spec(name, raw)
    return WorkflowSpec(
        name=name,
        graph=build_state_graph(spec),
        description=spec.description,
        graph_spec=spec,
    )


def runner_with(executor: FakeExecutor, **kw: Any) -> GraphWorkflowRunner:
    return GraphWorkflowRunner(executor_provider=lambda: executor, **kw)


# -- file format ---------------------------------------------------------------


def test_valid_file_parses_and_round_trips() -> None:
    spec = parse_graph_spec("doc_flow", DOC_FLOW)
    assert list(spec.nodes) == ["fetch", "classify", "check", "review", "save"]
    again = parse_graph_spec("doc_flow", spec.to_file())
    assert again.model_dump() == spec.model_dump()


def test_validator_reports_every_problem_at_once() -> None:
    bad = json.loads(json.dumps(DOC_FLOW))
    bad["edges"].append({"from": "classify", "to": "nowhere"})
    bad["nodes"]["check"]["rules"][0]["then"] = "missing"
    bad["nodes"]["save"]["args"] = {"meta": "{{typo.x}}"}
    bad["nodes"]["orphan"] = {"type": "llm", "prompt": "hi"}
    with pytest.raises(GraphSpecError) as exc:
        parse_graph_spec("doc-flow", bad)
    text = str(exc.value)
    for fragment in (
        "snake_case",
        "unknown target 'nowhere'",
        "'missing' is not a node",
        "unknown key 'typo'",
        "'orphan' is unreachable",
    ):
        assert fragment in text, fragment
    assert len(exc.value.errors) >= 5


def test_validator_checks_tools_when_given() -> None:
    with pytest.raises(GraphSpecError, match="tool 'documents_insert' is not available"):
        parse_graph_spec("doc_flow", DOC_FLOW, available_tools={"bucket_read"})


def test_validator_rejects_edges_out_of_branch_and_missing_start() -> None:
    raw = {
        "nodes": {"a": {"type": "branch", "else": "END"}},
        "edges": [{"from": "a", "to": "END"}],
    }
    with pytest.raises(GraphSpecError) as exc:
        parse_graph_spec("x", raw)
    assert "routes by its rules" in str(exc.value)
    assert "no edge from START" in str(exc.value)


def test_unknown_node_type_is_a_clear_error() -> None:
    with pytest.raises(GraphSpecError, match="nodes.a"):
        parse_graph_spec("x", {"nodes": {"a": {"type": "script"}}, "edges": []})


def test_load_graph_files_splits_valid_and_invalid(tmp_path) -> None:
    (tmp_path / "doc_flow.graph.json").write_text(json.dumps(DOC_FLOW))
    (tmp_path / "broken.graph.json").write_text("{not json")
    (tmp_path / "legacy.js").write_text("tools.output({result: 1})")
    valid, invalid = load_graph_files(tmp_path)
    assert list(valid) == ["doc_flow"]
    assert list(invalid) == ["broken"]
    assert "not valid JSON" in str(invalid["broken"])


# -- running -------------------------------------------------------------------


async def test_confident_run_completes_without_review() -> None:
    ex = FakeExecutor(confidence=0.95)
    runner = runner_with(ex)
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    result = await runner.start(spec, {"key": "inv.pdf"}, run_id="doc_flow:1")
    assert result.status == "completed"
    assert result.output == {"saved": {"sender": "ACME", "confidence": 0.95}}
    assert ex.tools_called() == ["bucket_read", "documents_insert"]
    llm = next(c for c in ex.calls if c.kind == "llm")
    assert "text of inv.pdf" in llm.payload["prompt"]
    record = await runner.index.get("doc_flow:1")
    assert record["status"] == "completed"


async def test_low_confidence_pauses_then_edit_resumes() -> None:
    ex = FakeExecutor(confidence=0.4)
    runner = runner_with(ex)
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    result = await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:2", trigger="telegram")
    assert result.status == "waiting"
    assert result.reviews[0]["message"] == "Sender ACME?"
    assert result.reviews[0]["data"] == {"classify": {"sender": "ACME", "confidence": 0.4}}
    assert "/workflows approve doc_flow:2" in result.to_text()
    assert ex.tools_called() == ["bucket_read"]  # nothing saved before review

    pending = await runner.index.pending_reviews()
    assert [p["run_id"] for p in pending] == ["doc_flow:2"]

    done = await runner.resume(
        spec,
        "doc_flow:2",
        {"action": "edit", "data": {"classify": {"sender": "Globex"}}, "by": "luca", "via": "ui"},
    )
    assert done.status == "completed"
    assert done.output == {"saved": {"sender": "Globex", "confidence": 0.4}}
    assert await runner.index.pending_reviews() == []


async def test_first_answer_wins() -> None:
    runner = runner_with(FakeExecutor(confidence=0.1))
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:3")
    review = await runner.claim_review(
        "doc_flow:3", {"action": "approve", "by": "luca", "via": "telegram"}
    )
    with pytest.raises(ReviewAlreadyResolved, match="already approved by luca via telegram"):
        await runner.claim_review("doc_flow:3", {"action": "reject", "via": "ui"})
    result = await runner.continue_review(spec, "doc_flow:3", review)
    assert result.status == "completed"


async def test_reject_ends_run_as_rejected() -> None:
    ex = FakeExecutor(confidence=0.1)
    runner = runner_with(ex)
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:4")
    result = await runner.resume(spec, "doc_flow:4", "reject")
    assert result.status == "rejected"
    assert "documents_insert" not in ex.tools_called()
    assert (await runner.index.get("doc_flow:4"))["status"] == "rejected"


async def test_bad_decision_is_refused() -> None:
    runner = runner_with(FakeExecutor(confidence=0.1))
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:5")
    with pytest.raises(ValueError, match="Unknown review action 'maybe'"):
        await runner.claim_review("doc_flow:5", {"action": "maybe"})


async def test_review_survives_a_new_runner_on_the_same_checkpointer() -> None:
    """A restart: new runner, same checkpointer + index → the pause resumes."""
    saver, index = InMemorySaver(), RunIndex()
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    first = runner_with(FakeExecutor(confidence=0.1), checkpointer=saver, index=index)
    await first.start(spec, {"key": "a.pdf"}, run_id="doc_flow:6")

    ex = FakeExecutor(confidence=0.1)
    second = runner_with(ex, checkpointer=saver, index=index)
    result = await second.resume(graph_spec_of("doc_flow", DOC_FLOW), "doc_flow:6", "approve")
    assert result.status == "completed"
    assert ex.tools_called() == ["documents_insert"]  # fetch + classify not re-run


async def test_crash_resume_continues_from_last_checkpoint() -> None:
    saver, index = InMemorySaver(), RunIndex()
    spec = graph_spec_of("doc_flow", DOC_FLOW)

    class Crashing(FakeExecutor):
        async def __call__(self, request: StepRequest) -> Any:
            if request.target == "documents_insert":
                raise RuntimeError("process died")
            return await super().__call__(request)

    first = runner_with(Crashing(), checkpointer=saver, index=index)
    with pytest.raises(RuntimeError):
        await first.start(spec, {"key": "a.pdf"}, run_id="doc_flow:7")
    # Simulate a kill (not a clean failure): the record is still "running".
    await index.update("doc_flow:7", status="running")

    ex = FakeExecutor()
    second = runner_with(ex, checkpointer=saver, index=index)
    resumed = await second.resume_incomplete(lambda name: spec if name == "doc_flow" else None)
    assert resumed == ["doc_flow:7"]
    assert ex.tools_called() == ["documents_insert"]
    assert (await index.get("doc_flow:7"))["status"] == "completed"


async def test_restart_applies_an_answer_claimed_before_a_crash() -> None:
    """Answer recorded, process died before the run continued → restart applies it."""
    saver, index = InMemorySaver(), RunIndex()
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    first = runner_with(FakeExecutor(confidence=0.1), checkpointer=saver, index=index)
    await first.start(spec, {"key": "a.pdf"}, run_id="doc_flow:10")
    await first.claim_review("doc_flow:10", {"action": "approve", "via": "telegram"})
    assert (await index.get("doc_flow:10"))["status"] == "running"

    ex = FakeExecutor()
    second = runner_with(ex, checkpointer=saver, index=index)
    assert await second.resume_incomplete(lambda _: spec) == ["doc_flow:10"]
    assert ex.tools_called() == ["documents_insert"]
    assert (await index.get("doc_flow:10"))["status"] == "completed"


async def test_failed_run_is_recorded() -> None:
    class Broken(FakeExecutor):
        async def __call__(self, request: StepRequest) -> Any:
            raise RuntimeError("bucket offline")

    runner = runner_with(Broken())
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    with pytest.raises(RuntimeError):
        await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:8")
    record = await runner.index.get("doc_flow:8")
    assert record["status"] == "failed"
    assert "bucket offline" in record["error"]


async def test_get_run_lists_each_step() -> None:
    runner = runner_with(FakeExecutor())
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:9")
    run = await runner.get_run(spec, "doc_flow:9")
    nodes = [s["node"] for s in run["steps"]]
    assert nodes == ["fetch", "classify", "check", "save"]
    assert run["steps"][0]["result"] == {"data": {"fetch": "text of a.pdf"}}
    assert run["next"] == []


async def test_parallel_fan_out_and_join() -> None:
    raw = {
        "nodes": {
            "a": {"type": "tool", "tool": "bucket_read", "args": {"key": "a"}},
            "b": {"type": "tool", "tool": "bucket_read", "args": {"key": "b"}},
            "join": {"type": "llm", "prompt": "{{a}} + {{b}}"},
        },
        "edges": [
            {"from": "START", "to": "a"},
            {"from": "START", "to": "b"},
            {"from": ["a", "b"], "to": "join"},
        ],
        "output": "join",
    }

    class Echo(FakeExecutor):
        async def __call__(self, request: StepRequest) -> Any:
            if request.kind == "llm":
                return request.payload["prompt"]
            return await super().__call__(request)

    runner = runner_with(Echo())
    result = await runner.start(graph_spec_of("fan", raw), {}, run_id="fan:1")
    assert result.output == "text of a + text of b"


def test_mermaid_drawing_shows_nodes() -> None:
    runner = GraphWorkflowRunner()
    drawing = runner.mermaid(graph_spec_of("doc_flow", DOC_FLOW))
    for node in ("fetch", "classify", "review", "save"):
        assert node in drawing


# -- Python-authored graphs ----------------------------------------------------


class DocState(TypedDict, total=False):
    key: str
    sender: str
    approved: bool


async def _classify(state: DocState) -> dict:
    text = await steps().tool("bucket_read", key=state["key"])
    return {"sender": f"from {text}"}


def _review(state: DocState) -> dict:
    decision = request_review("OK?", data={"sender": state["sender"]}, node="review")
    return {"approved": decision["action"] != "reject"}


def python_graph() -> WorkflowSpec:
    builder = StateGraph(DocState)
    builder.add_node("classify", _classify)
    builder.add_node("review", _review)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "review")
    builder.add_edge("review", END)
    return WorkflowSpec(name="py_doc", graph=builder)


async def test_python_graph_uses_steps_and_reviews() -> None:
    runner = runner_with(FakeExecutor())
    spec = python_graph()
    result = await runner.start(spec, {"key": "k1"}, run_id="py_doc:1")
    assert result.status == "waiting"
    assert result.reviews[0]["data"] == {"sender": "from text of k1"}
    done = await runner.resume(spec, "py_doc:1", "approve")
    assert done.output == {"key": "k1", "sender": "from text of k1", "approved": True}


def test_steps_outside_a_run_is_a_clear_error() -> None:
    from langclaw.workflows.executor import WorkflowStepError

    with pytest.raises(WorkflowStepError, match="only available inside"):
        steps()
