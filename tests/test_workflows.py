"""Workflow building blocks: registry, step executor, agent tools, config, RBAC, progress.

Graph execution (files, runs, reviews, resume) is covered in test_graph_workflows.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from typing_extensions import TypedDict

from langclaw.config.schema import WorkflowsConfig
from langclaw.workflows import WorkflowRegistry, WorkflowRuntime, WorkflowSpec


class _S(TypedDict, total=False):
    n: int


def _graph() -> StateGraph:
    builder = StateGraph(_S)
    builder.add_node("inc", lambda s: {"n": s.get("n", 0) + 1})
    builder.add_edge(START, "inc")
    builder.add_edge("inc", END)
    return builder


def _spec(name: str = "echo", **kw) -> WorkflowSpec:
    return WorkflowSpec(name=name, graph=_graph(), **kw)


# -- registry ------------------------------------------------------------------


def test_registry_register_and_get() -> None:
    reg = WorkflowRegistry()
    spec = reg.register(_spec(description="d"))
    assert reg.get("echo") is spec
    assert reg.names() == ["echo"] and len(reg) == 1 and "echo" in reg
    assert spec.source == "code"


def test_registry_rejects_duplicate_reserved_and_empty() -> None:
    reg = WorkflowRegistry()
    reg.register(_spec())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_spec())
    with pytest.raises(ValueError, match="collides"):
        reg.register(_spec("web_search"), reserved_names={"web_search"})
    with pytest.raises(ValueError, match="non-empty"):
        reg.register(_spec(" "))


def test_registry_version_bumps_on_register_and_unregister() -> None:
    reg = WorkflowRegistry()
    reg.register(_spec())
    v = reg.version
    assert reg.unregister("echo") is True
    assert reg.version == v + 1
    assert reg.unregister("echo") is False


def test_spec_requires_an_uncompiled_graph() -> None:
    with pytest.raises(ValueError, match="uncompiled LangGraph StateGraph"):
        WorkflowSpec(name="x", graph=None)
    with pytest.raises(ValueError, match="uncompiled LangGraph StateGraph"):
        WorkflowSpec(name="x", graph=lambda s: s)


def test_validate_input_coerces_json_string() -> None:
    class Inp(BaseModel):
        topic: str

    spec = _spec(input_model=Inp)
    assert spec.validate_input('{"topic": "x"}') == Inp(topic="x")
    assert spec.validate_input({"topic": "y"}).topic == "y"
    assert _spec().validate_input("raw") == "raw"


# -- runtime -------------------------------------------------------------------


async def test_runtime_runs_a_python_graph() -> None:
    runtime = WorkflowRuntime(WorkflowsConfig(enabled=True))
    runtime.set_executor_factory(lambda _: None)
    result = await runtime.run_graph(_spec(), {"n": 1}, run_id="echo:1")
    assert (result.status, result.output) == ("completed", {"n": 2})


async def test_runtime_global_concurrency_ceiling() -> None:
    """max_concurrent_runs bounds simultaneous runs across workflows."""
    active = peak = 0

    async def slow(state: _S) -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"n": 1}

    builder = StateGraph(_S)
    builder.add_node("slow", slow)
    builder.add_edge(START, "slow")
    builder.add_edge("slow", END)
    spec = WorkflowSpec(name="slow", graph=builder)
    runtime = WorkflowRuntime(WorkflowsConfig(enabled=True, max_concurrent_runs=2))
    runtime.set_executor_factory(lambda _: None)
    await asyncio.gather(*(runtime.run_graph(spec, {}, run_id=f"slow:{i}") for i in range(6)))
    assert peak == 2


async def test_runtime_timeout_marks_run_failed() -> None:
    async def hang(state: _S) -> dict:
        await asyncio.sleep(5)
        return {}

    builder = StateGraph(_S)
    builder.add_node("hang", hang)
    builder.add_edge(START, "hang")
    builder.add_edge("hang", END)
    runtime = WorkflowRuntime(WorkflowsConfig(enabled=True))
    runtime.set_executor_factory(lambda _: None)
    with pytest.raises(asyncio.TimeoutError):
        await runtime.run_graph(
            WorkflowSpec(name="hang", graph=builder, timeout_s=0.05), {}, run_id="hang:1"
        )
    assert (await runtime.graph_runner.index.get("hang:1"))["status"] == "failed"


async def test_runtime_emits_a_progress_line_per_node() -> None:
    from langclaw.workflows import reset_progress_sink, set_progress_sink

    events: list[dict] = []
    token = set_progress_sink(events.append)
    try:
        runtime = WorkflowRuntime(WorkflowsConfig(enabled=True))
        runtime.set_executor_factory(lambda _: None)
        await runtime.run_graph(_spec(), {"n": 0}, run_id="echo:p")
    finally:
        reset_progress_sink(token)
    assert [(e["kind"], e["phase"]) for e in events] == [("phase", "inc")]


def test_workflows_config_defaults() -> None:
    cfg = WorkflowsConfig()
    assert cfg.enabled is False
    assert cfg.max_concurrent_runs == 16
    assert cfg.max_steps_per_run == 1000


# -- agent tools + prompt -------------------------------------------------------


async def test_workflow_tool_runs_and_returns_output() -> None:
    from langclaw.workflows import make_workflow_tools

    reg = WorkflowRegistry()
    reg.register(_spec(description="Add one"))
    runtime = WorkflowRuntime(WorkflowsConfig(enabled=True))
    runtime.set_executor_factory(lambda _: None)
    (tool,) = make_workflow_tools(reg, runtime)
    assert tool.name == "workflow_echo" and tool.description == "Add one"
    assert '"n": 3' in await tool.ainvoke({"workflow_input": {"n": 2}})


async def test_workflow_tool_returns_errors_as_text() -> None:
    from langclaw.workflows import make_workflow_tools

    class Inp(BaseModel):
        n: int

    reg = WorkflowRegistry()
    reg.register(_spec(input_model=Inp))
    runtime = WorkflowRuntime(WorkflowsConfig(enabled=True))
    (tool,) = make_workflow_tools(reg, runtime)
    text = await tool.ainvoke({"workflow_input": {"n": "not a number"}})
    assert text.startswith("Error: workflow 'echo' failed")


def test_workflow_system_prompt_lists_registered_workflows() -> None:
    from langclaw.workflows import workflow_system_prompt

    reg = WorkflowRegistry()
    reg.register(_spec(description="Add one"))
    prompt = workflow_system_prompt(reg)
    assert "workflow_echo — Add one" in prompt
    assert "pause for human review" in prompt
    assert ".js" not in prompt


def test_resolve_workflow_ptc_names() -> None:
    from langclaw.workflows import resolve_workflow_ptc_names

    reg = WorkflowRegistry()
    reg.register(_spec("b"))
    reg.register(_spec("a"))
    names = resolve_workflow_ptc_names(reg, workflows_config=WorkflowsConfig(enabled=True))
    assert names == ["workflow_a", "workflow_b"]
    assert resolve_workflow_ptc_names(reg, workflows_config=WorkflowsConfig()) == []


# -- kept from the previous suite ----------------------------------------------


def test_workflows_config_on_root_and_env(monkeypatch):
    from langclaw.config.schema import LangclawConfig

    assert LangclawConfig(workflows={"enabled": False}).workflows.enabled is False
    monkeypatch.setenv("LANGCLAW__WORKFLOWS__ENABLED", "true")
    monkeypatch.setenv("LANGCLAW__WORKFLOWS__MAX_CONCURRENT_RUNS", "3")
    cfg = LangclawConfig()
    assert cfg.workflows.enabled is True
    assert cfg.workflows.max_concurrent_runs == 3


def test_role_config_workflows_default_empty():
    from langclaw.config.schema import RoleConfig

    assert RoleConfig().workflows == []
    assert RoleConfig(workflows=["digest"]).workflows == ["digest"]


def test_allowed_workflow_names_default_deny():
    from langclaw.config.schema import PermissionsConfig, RoleConfig
    from langclaw.middleware.permissions import allowed_workflow_names

    cfg = PermissionsConfig(
        enabled=True,
        roles={
            "viewer": RoleConfig(tools=["*"]),  # no workflows → deny
            "power": RoleConfig(workflows=["digest"]),
            "admin": RoleConfig(workflows=["*"]),
        },
    )
    universe = ["digest", "report"]
    assert allowed_workflow_names(cfg, "viewer", universe) == set()
    assert allowed_workflow_names(cfg, "power", universe) == {"digest"}
    assert allowed_workflow_names(cfg, "admin", universe) == set(universe)
    assert allowed_workflow_names(cfg, "ghost", universe) == set()


def test_app_role_carries_subagent_and_workflow_axes():
    """``app.role()`` must accept the ``subagents`` and ``workflows`` axes so
    operators never have to reach into ``config.permissions.roles`` (which is
    only populated at build time). The effective config reflects all three."""
    from langclaw import Langclaw

    app = Langclaw()
    app.role("analyst", tools=["*"], subagents=["writer"], workflows=["research"])

    cfg = app._build_effective_config()
    role = cfg.permissions.roles["analyst"]
    assert role.tools == ["*"]
    assert role.subagents == ["writer"]
    assert role.workflows == ["research"]


def test_app_role_merges_axes_across_calls():
    """Repeated ``app.role()`` calls merge each axis, deduping order-stably."""
    from langclaw import Langclaw

    app = Langclaw()
    app.role("analyst", tools=["web_search"], workflows=["research"])
    app.role("analyst", tools=["web_search", "web_fetch"], workflows=["digest"])

    role = app._build_effective_config().permissions.roles["analyst"]
    assert role.tools == ["web_search", "web_fetch"]
    assert role.workflows == ["research", "digest"]


def _fake_tool(name: str, fn):
    """A minimal async tool stand-in exposing ``.name`` and ``.ainvoke``.

    The executor only reads ``.name`` and awaits ``.ainvoke(args_dict)``, so a
    tiny shim avoids langchain's docstring/schema requirements for test fns.
    """

    class _FakeTool:
        def __init__(self) -> None:
            self.name = name

        async def ainvoke(self, args: dict):
            return await fn(**args)

    return _FakeTool()


@pytest.mark.asyncio
async def test_toolset_executor_invokes_live_tool():
    from langclaw.workflows.executor import build_toolset_executor

    async def echo(text: str) -> str:
        return f"echo:{text}"

    executor = build_toolset_executor([_fake_tool("echo", echo)])

    from langclaw.workflows.executor import StepRequest

    req = StepRequest(kind="tool", target="echo", payload={"text": "hi"})
    out = await executor(req)
    assert "echo:hi" in str(out)


@pytest.mark.asyncio
async def test_toolset_executor_unknown_tool_raises_step_error():
    from langclaw.workflows.executor import StepRequest, WorkflowStepError, build_toolset_executor

    executor = build_toolset_executor([])
    req = StepRequest(kind="tool", target="nope", payload={})
    with pytest.raises(WorkflowStepError, match="(?i)tool 'nope'"):
        await executor(req)


@pytest.mark.asyncio
async def test_toolset_executor_invokes_subagent_runnable():
    """``ctx.subagent`` invokes the subagent's compiled graph directly and returns
    its final AI text — no ``task`` tool, which needs an injected ToolRuntime the
    out-of-graph workflow executor can't supply."""
    from langchain_core.messages import AIMessage, HumanMessage

    from langclaw.workflows.executor import StepRequest, build_toolset_executor

    seen = {}

    class _FakeRunnable:
        async def ainvoke(self, state, config=None):
            seen["messages"] = state["messages"]
            return {"messages": [*state["messages"], AIMessage(content="deep finding")]}

    executor = build_toolset_executor([], subagent_runnables={"researcher": _FakeRunnable()})
    req = StepRequest(kind="subagent", target="researcher", payload="go deep")
    out = await executor(req)
    assert out == "deep finding"
    # the prompt was handed to the subagent as a fresh user message (isolated context)
    assert isinstance(seen["messages"][0], HumanMessage)
    assert seen["messages"][0].content == "go deep"


@pytest.mark.asyncio
async def test_toolset_executor_unknown_subagent_raises_clear_error():
    from langclaw.workflows.executor import StepRequest, WorkflowStepError, build_toolset_executor

    executor = build_toolset_executor([], subagent_runnables={"known": object()})
    req = StepRequest(kind="subagent", target="ghost", payload="x")
    with pytest.raises(WorkflowStepError, match="(?i)registered subagent"):
        await executor(req)


class _FakeChatModel:
    """A chat model stand-in for an llm step tests: records the messages it was asked
    and returns either plain text or, via with_structured_output, a fixed object."""

    def __init__(self, text="ok", structured=None):
        self._text = text
        self._structured = structured
        self.seen_messages = None
        self.structured_schema = None

    async def ainvoke(self, messages, *a, **k):
        from langchain_core.messages import AIMessage

        self.seen_messages = messages
        return AIMessage(content=self._text)

    def with_structured_output(self, schema, *a, **k):
        self.structured_schema = schema
        outer = self

        class _Structured:
            async def ainvoke(self, messages, *a, **k):
                outer.seen_messages = messages
                return outer._structured

        return _Structured()


@pytest.mark.asyncio
async def test_toolset_executor_llm_plain_text():
    from langchain_core.messages import HumanMessage

    from langclaw.workflows.executor import StepRequest, build_toolset_executor

    model = _FakeChatModel(text="it's a bug")
    executor = build_toolset_executor([], default_model=model)
    req = StepRequest(kind="llm", target="", payload={"prompt": "classify this"})
    out = await executor(req)
    assert out == "it's a bug"
    # the prompt was sent as a user message
    sent = model.seen_messages
    assert any(isinstance(m, HumanMessage) or m == ("user", "classify this") for m in sent)


@pytest.mark.asyncio
async def test_toolset_executor_llm_structured_schema():
    from langclaw.workflows.executor import StepRequest, build_toolset_executor

    class Verdict(BaseModel):
        label: str

    model = _FakeChatModel(structured=Verdict(label="real"))
    executor = build_toolset_executor([], default_model=model)
    req = StepRequest(kind="llm", target="", payload={"prompt": "judge"}, schema=Verdict)
    out = await executor(req)
    assert isinstance(out, Verdict) and out.label == "real"
    assert model.structured_schema is Verdict


@pytest.mark.asyncio
async def test_toolset_executor_llm_structured_json_fallback():
    """When the provider lacks native structured output, an llm step(schema=...) falls
    back to a JSON instruction + parse, so it works on any endpoint."""
    from langchain_core.messages import AIMessage

    from langclaw.workflows.executor import StepRequest, build_toolset_executor

    class Out(BaseModel):
        label: str
        score: int

    class _NoNativeStructured:
        def with_structured_output(self, schema, *a, **k):
            raise RuntimeError("provider has no native structured output")

        async def ainvoke(self, messages, *a, **k):
            # the fallback call returns JSON (in a fence, to exercise extraction)
            return AIMessage(content='```json\n{"label": "bug", "score": 8}\n```')

    executor = build_toolset_executor([], default_model=_NoNativeStructured())
    req = StepRequest(kind="llm", target="", payload={"prompt": "judge"}, schema=Out)
    out = await executor(req)
    assert isinstance(out, Out) and out.label == "bug" and out.score == 8


@pytest.mark.asyncio
async def test_toolset_executor_llm_no_model_raises():
    from langclaw.workflows.executor import StepRequest, WorkflowStepError, build_toolset_executor

    executor = build_toolset_executor([], default_model=None)
    req = StepRequest(kind="llm", target="", payload={"prompt": "x"})
    with pytest.raises(WorkflowStepError, match="(?i)model"):
        await executor(req)


@pytest.mark.asyncio
async def test_toolset_executor_llm_model_override_resolved():
    from langclaw.workflows.executor import StepRequest, build_toolset_executor

    resolved = {}

    def resolver(spec):
        resolved["spec"] = spec
        return _FakeChatModel(text="from override")

    executor = build_toolset_executor(
        [], default_model=_FakeChatModel(text="from default"), model_resolver=resolver
    )
    req = StepRequest(kind="llm", target="openai:gpt-4.1", payload={"prompt": "x"})
    out = await executor(req)
    assert out == "from override"
    assert resolved["spec"] == "openai:gpt-4.1"


def _wf_tool(name: str):
    return SimpleNamespace(name=name)


def _run_model_call(mw, tools, user_role):
    """Drive a wrap_model_call middleware once and return the tools the handler saw.

    Mirrors the proven pattern in ``test_interpreter`` (SimpleNamespace request
    with an ``override`` lambda, executed via ``asyncio.run``) so the harness
    matches langchain's handler contract exactly.
    """
    import asyncio

    runtime = SimpleNamespace(context=SimpleNamespace(user_role=user_role))
    request = SimpleNamespace(
        runtime=runtime,
        tools=tools,
        override=lambda **kw: SimpleNamespace(**{"tools": tools, "runtime": runtime, **kw}),
    )
    captured = {}

    async def handler(req):
        captured["tools"] = req.tools
        return "ok"

    asyncio.run(mw.awrap_model_call(request, handler))
    return {t.name for t in captured["tools"]}


def test_capability_filter_governs_workflow_axis():
    """The unified seam (issue #37) filters the workflow axis in the same pass."""
    from langclaw.config.schema import PermissionsConfig, RoleConfig
    from langclaw.middleware.permissions import build_capability_filter_middleware

    cfg = PermissionsConfig(
        enabled=True,
        roles={"power": RoleConfig(tools=["*"], workflows=["digest"])},
    )
    mw = build_capability_filter_middleware(cfg)

    tools = [_wf_tool("web_search"), _wf_tool("workflow_digest"), _wf_tool("workflow_secret")]
    names = _run_model_call(mw, tools, "power")
    # tool axis (tools=["*"]) keeps web_search; workflow axis keeps only digest.
    assert "web_search" in names
    assert "workflow_digest" in names
    assert "workflow_secret" not in names


def test_capability_filter_handles_tool_and_workflow_axes_together():
    """One filter applies both axes: viewer keeps only web_search; default-deny
    strips the un-granted workflow even though the tool axis is separate."""
    from langclaw.config.schema import PermissionsConfig, RoleConfig
    from langclaw.middleware.permissions import build_capability_filter_middleware

    cfg = PermissionsConfig(enabled=True, roles={"viewer": RoleConfig(tools=["web_search"])})
    mw = build_capability_filter_middleware(cfg)

    tools = [_wf_tool("web_search"), _wf_tool("delete_file"), _wf_tool("workflow_digest")]
    names = _run_model_call(mw, tools, "viewer")
    # delete_file stripped (tool axis); workflow_digest stripped (workflow axis,
    # default-deny — viewer was granted no workflows).
    assert names == {"web_search"}


def test_progress_sink_set_emit_reset():
    from langclaw.workflows.progress import emit_progress, reset_progress_sink, set_progress_sink

    got: list = []
    token = set_progress_sink(lambda e: got.append(e))
    try:
        emit_progress({"kind": "phase", "phase": "x"})
    finally:
        reset_progress_sink(token)
    assert got == [{"kind": "phase", "phase": "x"}]
    # after reset there is no sink — emit is a no-op, never raises
    emit_progress({"kind": "phase", "phase": "y"})
    assert got == [{"kind": "phase", "phase": "x"}]


def test_progress_sink_swallows_errors():
    from langclaw.workflows.progress import emit_progress, reset_progress_sink, set_progress_sink

    def boom(_e):
        raise RuntimeError("sink blew up")

    token = set_progress_sink(boom)
    try:
        emit_progress({"k": 1})  # must NOT propagate into the workflow
    finally:
        reset_progress_sink(token)


def test_render_log_progress():
    from langclaw.workflows.progress import render_workflow_progress

    out = render_workflow_progress({"kind": "log", "workflow": "r", "message": "step 2 done"})
    assert out is not None and "step 2 done" in out


def test_workflow_system_prompt_empty_registry_is_blank():
    from langclaw.workflows import WorkflowRegistry, workflow_system_prompt

    assert workflow_system_prompt(WorkflowRegistry()) == ""
