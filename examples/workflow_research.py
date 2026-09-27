"""
Workflow Research — a bot whose agent can run a checkpointed LangGraph workflow
that pauses for your approval before it delivers.

Demonstrates
------------
- ``app.workflow(name, graph=builder)`` — register a LangGraph ``StateGraph``
- Pydantic-typed input — the run boundary validates the workflow's arguments
- ``Send`` fan-out — one web search per angle, in parallel, each checkpointed
- ``steps()`` — nodes reach langclaw's tools and model (``tool`` / ``llm``)
- ``request_review()`` — human-in-the-loop: the run pauses until you approve,
  edit, or reject the draft (from Telegram with ``/workflows approve <run_id>``,
  or from the UI). The paused run survives restarts.
- RBAC ``workflows`` — the default-deny permission axis, per role

How the agent invokes it
------------------------
The workflow is surfaced to the agent as a tool named ``workflow_research``.
Ask in plain language ("research solid-state batteries"); the model calls
``workflow_research({"topic": "..."})``, the run searches and drafts, then tells
you it is waiting for review. Answer with ``/workflows approve <run_id>`` (or
``edit <run_id> {"draft": "..."}`` / ``reject``) and the approved brief arrives.
You can also start it directly: ``/workflows run research {"topic": "..."}``.

Run
---
1. Copy ``.env.example`` to ``.env`` and fill in an LLM provider key and a
   channel token (Telegram or Discord).
2. ``pip install langclaw[telegram]``   (or ``langclaw[discord]``, etc.)
3. Enable workflows — they are **off by default**::

       export LANGCLAW__WORKFLOWS__ENABLED=true

4. ``python examples/workflow_research.py``
"""

from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from langclaw import Langclaw
from langclaw.workflows import request_review, steps

app = Langclaw()

# RBAC: workflows are default-deny once roles exist. Give everyone the analyst role.
app.role("analyst", tools=["*"], workflows=["research"])
app.config.permissions.default_role = "analyst"


class Brief(BaseModel):
    topic: str = Field(description="What to research.")
    angles: list[str] = Field(
        default=["overview", "risks", "recent news"],
        description="Angles to search in parallel.",
    )


class ResearchState(TypedDict, total=False):
    topic: str
    angles: list[str]
    findings: Annotated[list[dict], operator.add]
    draft: str
    approved: bool
    brief: str


def fan_out(state: ResearchState) -> list[Send]:
    return [Send("search", {"topic": state["topic"], "angle": a}) for a in state["angles"]]


async def search(task: dict) -> dict:
    hits = await steps().tool("web_search", query=f"{task['topic']} {task['angle']}", n=3)
    return {"findings": [{"angle": task["angle"], "hits": hits}]}


async def draft(state: ResearchState) -> dict:
    text = await steps().llm(
        f"Topic: {state['topic']}\nSearch results by angle:\n{state['findings']}",
        system="Write a crisp research brief with a section per angle. Cite URLs.",
    )
    return {"draft": text}


def review(state: ResearchState) -> dict:
    # Side effects belong AFTER request_review: the node re-runs from the top on resume.
    decision = request_review(
        f"Approve the research brief on {state['topic']!r}?",
        data={"draft": state["draft"]},
        editable="draft",
        node="review",
    )
    if decision["action"] == "reject":
        return {"approved": False, "brief": "Brief rejected at review."}
    text = decision["data"].get("draft", state["draft"])
    return {"approved": True, "brief": text}


builder = StateGraph(ResearchState)
builder.add_node("search", search)
builder.add_node("draft", draft)
builder.add_node("review", review)
builder.add_conditional_edges(START, fan_out, ["search"])
builder.add_edge("search", "draft")
builder.add_edge("draft", "review")
builder.add_edge("review", END)

app.workflow(
    "research",
    graph=builder,
    input=Brief,
    output_key="brief",
    max_concurrency=4,
    description=(
        "Research a topic from several angles in parallel (web search per angle), draft "
        "a cited brief, and pause for the user's approval before delivering it. Prefer "
        "this over ad-hoc searches when the user wants a multi-angle brief."
    ),
)

if __name__ == "__main__":
    app.run()
