"""
Step executor — how workflow nodes reach langclaw's tools, models, and subagents.

A graph workflow node never imports the app: it calls
:func:`langclaw.workflows.graph.steps` (``steps().tool(...)``, ``.llm(...)``,
``.subagent(...)``), which turns each call into a :class:`StepRequest` and hands
it to a :data:`StepExecutor`. In production the executor is
:func:`build_toolset_executor` over the default agent's live toolset; in tests it
is a plain ``async def (request) -> result``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

StepExecutor = Callable[["StepRequest"], Awaitable[Any]]


class WorkflowStepError(RuntimeError):
    """A workflow step could not run (missing tool/subagent/model, bad output)."""


@dataclass(slots=True)
class StepRequest:
    """One unit of work a workflow node asks the executor to perform.

    Attributes:
        kind:    ``"tool"`` | ``"llm"`` | ``"subagent"``.
        target:  Tool name, model override (``""`` ⇒ default), or subagent type.
        payload: Tool kwargs (tool), ``{"prompt", "system"}`` (llm), or the
                 prompt string (subagent).
        schema:  Optional Pydantic model an ``llm`` result must validate against.
    """

    kind: str
    target: str
    payload: Any
    schema: type | None = None


def build_toolset_executor(
    available_tools: list[Any],
    *,
    subagent_runnables: dict[str, Any] | None = None,
    default_model: Any | None = None,
    model_resolver: Callable[[str], Any] | None = None,
) -> StepExecutor:
    """Return a :class:`StepExecutor` backed by a live toolset.

    Args:
        available_tools: The tools (objects with ``.name`` and ``.ainvoke``)
            the workflow's steps may reach.  Typically the same role-filtered
            toolset the agent itself was built with.
        subagent_runnables: ``{subagent_type: compiled graph}`` the workflow may
            delegate to via ``steps().subagent``.  Each graph is invoked directly
            (``ainvoke({"messages": [HumanMessage(prompt)]})``) — not via the
            ``task`` tool — so it works from outside the agent graph.  ``None`` ⇒
            no subagent is reachable and ``steps().subagent`` raises a clear error.
        default_model: The chat model ``an llm step`` calls when no per-call ``model``
            override is given.  ``None`` ⇒ ``an llm step`` raises a clear error.
        model_resolver: ``(model_spec) -> chat model`` used to resolve a per-call
            ``an llm step(model=...)`` override.  ``None`` ⇒ overrides aren't allowed.

    Returns:
        An async ``(StepRequest) -> result`` callable:

        - ``kind == "tool"``  → ``tool.ainvoke(payload_dict)``.
        - ``kind == "subagent"`` → invoke ``subagent_runnables[target]`` with the
          payload as a user message; return its final AI text.
        - ``kind == "llm"`` → one model call (no tools, no loop); plain text, or a
          validated object when the step carries a ``schema``.

    Raises (inside the returned callable):
        WorkflowStepError: when a referenced tool is absent, a subagent is not
            registered, no model is configured for ``an llm step``, or a named-``agent``
            step is requested (unsupported).
    """
    by_name: dict[str, Any] = {}
    for t in available_tools:
        name = getattr(t, "name", None)
        if name:
            by_name[name] = t
    runnables: dict[str, Any] = subagent_runnables or {}

    async def _executor(request: StepRequest) -> Any:
        if request.kind == "tool":
            tool = by_name.get(request.target)
            if tool is None:
                raise WorkflowStepError(
                    f"Workflow step referenced tool {request.target!r} which is "
                    "not available to this run."
                )
            args = request.payload if isinstance(request.payload, dict) else {}
            return await tool.ainvoke(args)

        if request.kind == "llm":
            return await _run_llm_step(request, default_model, model_resolver)

        if request.kind == "subagent":
            runnable = runnables.get(request.target)
            if runnable is None:
                available = ", ".join(sorted(runnables)) or "none"
                raise WorkflowStepError(
                    f"Workflow step requested subagent {request.target!r}, which is not "
                    f"a registered subagent (available: {available}). Register it with "
                    "app.subagent(...)."
                )
            from langchain_core.messages import HumanMessage

            result = await runnable.ainvoke(
                {"messages": [HumanMessage(content=str(request.payload))]}
            )
            return _subagent_reply_text(result)

        raise WorkflowStepError(f"Unknown workflow step kind: {request.kind!r}")

    return _executor


def _subagent_reply_text(result: Any) -> str:
    """Extract a subagent graph's final reply: the last non-empty AI message text.

    Mirrors how deepagents' ``task`` tool reduces a subagent result — walk back to
    the last :class:`AIMessage` with text (a trailing empty ``end_turn`` message is
    skipped).  Returns ``""`` when the subagent produced no text.
    """
    from langchain_core.messages import AIMessage

    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        content = msg.content
        if isinstance(content, str):
            if content.strip():
                return content.strip()
            continue
        if isinstance(content, list):  # content blocks → join the text parts
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = " ".join(parts).strip()
            if text:
                return text
    return ""


async def _run_llm_step(
    request: StepRequest,
    default_model: Any | None,
    model_resolver: Callable[[str], Any] | None,
) -> Any:
    """Execute a ``an llm step`` step: one model call, optionally schema-validated.

    ``request.target`` is the per-call model spec (``""`` ⇒ default); ``request.payload``
    is ``{"prompt", "system"}``; ``request.schema`` (when set) forces structured output.
    """
    payload = request.payload if isinstance(request.payload, dict) else {"prompt": request.payload}

    model = default_model
    if request.target:
        if model_resolver is None:
            raise WorkflowStepError(
                f"an llm step requested model {request.target!r} but no model resolver is "
                "configured for this run."
            )
        model = model_resolver(request.target)
    if model is None:
        raise WorkflowStepError(
            "An llm step has no model to call — none was configured for this run."
        )

    messages: list[Any] = []
    system = payload.get("system")
    if system:
        messages.append(("system", system))
    messages.append(("user", payload.get("prompt", "")))

    if request.schema is None:
        return _ai_text(await model.ainvoke(messages))

    # Structured output. Prefer the provider's native path; fall back to a JSON
    # instruction + parse so a structured llm step works even on endpoints that don't
    # support native structured output (e.g. some OpenAI-compatible proxies).
    try:
        return await model.with_structured_output(request.schema).ainvoke(messages)
    except Exception as native_exc:  # noqa: BLE001 — fall back; hide raw provider errors
        logger.debug(f"Native structured output failed ({native_exc}); using JSON fallback")
        return await _llm_json_fallback(model, messages, request.schema, native_exc)


async def _llm_json_fallback(
    model: Any, messages: list[Any], schema: type, native_exc: Exception
) -> Any:
    """Get structured output by instructing the model to emit JSON, then validating."""
    schema_json = json.dumps(schema.model_json_schema())
    instruction = (
        "Respond with ONLY a single JSON object matching this JSON Schema — no prose, "
        f"no markdown fences:\n{schema_json}"
    )
    reply = _ai_text(await model.ainvoke([*messages, ("user", instruction)]))
    try:
        obj = json.loads(_extract_json(reply))
    except (ValueError, TypeError) as exc:
        raise WorkflowStepError(
            f"an llm step could not produce structured output for schema "
            f"{getattr(schema, '__name__', schema)!r}: native structured output failed "
            f"({native_exc}) and the JSON fallback reply was not parseable."
        ) from exc
    return schema.model_validate(obj)


def _extract_json(text: str) -> str:
    """Pull a JSON object out of a model reply (strip ``` fences, slice to braces)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    return t[start : end + 1] if start != -1 and end > start else t


def _ai_text(msg: Any) -> str:
    """Extract plain text from a single chat-model reply (``AIMessage``)."""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(parts).strip()
    return str(content).strip()
