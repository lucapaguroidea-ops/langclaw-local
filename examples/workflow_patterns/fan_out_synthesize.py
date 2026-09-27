"""
Pattern: FAN-OUT-AND-SYNTHESIZE  —  split work across parallel branches, then merge.

Blog: "Fan-out-and-synthesize"

Real job: a competitive-landscape brief. Research each contender in its OWN subagent
— an isolated context with its own web_search — so no contender's findings colour
another's. Then a single synthesis step folds the per-contender notes into one
comparison across the dimensions you care about. Branch failures are isolated: one
scout erroring is recorded as "no usable findings" instead of sinking the brief.

LangGraph shape: ``Send`` fans out one ``scout`` task per contender (each its own
checkpointed task, run in parallel); their notes collect through an ``operator.add``
reducer; ``synthesize`` runs once all scouts are done.

    /workflows run landscape {"subject": "agent framework",
        "contenders": ["LangGraph", "CrewAI", "AutoGen"],
        "dimensions": ["control", "durability", "learning curve"]}
"""

from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from examples.workflow_patterns._app import make_app
from langclaw.workflows import steps

_COMPARE_SYS = (
    "You are an industry analyst. You are given research notes for several competing "
    "options. Produce ONE markdown comparison table: a row per option, a column per "
    "requested dimension, terse cells grounded ONLY in the notes. Add a one-line "
    "'Bottom line' under the table. Do not invent facts."
)


class Landscape(BaseModel):
    subject: str = Field(description="What the contenders are competing to be.")
    contenders: list[str] = Field(description="The things to compare (2–6).")
    dimensions: list[str] = Field(
        default=["strengths", "weaknesses", "best for"],
        description="The axes to compare on.",
    )


class LandscapeState(TypedDict, total=False):
    subject: str
    contenders: list[str]
    dimensions: list[str]
    notes: Annotated[list[dict], operator.add]
    report: str


class ScoutTask(TypedDict):
    name: str
    subject: str
    dimensions: list[str]


def fan_out(state: LandscapeState) -> list[Send]:
    return [
        Send(
            "scout",
            {"name": n, "subject": state["subject"], "dimensions": state["dimensions"]},
        )
        for n in state["contenders"]
    ]


async def scout(task: ScoutTask) -> dict:
    try:
        text = await steps().subagent(
            "scout",
            f"Research '{task['name']}' as a {task['subject']}. "
            f"Focus: {', '.join(task['dimensions'])}.",
        )
    except Exception:  # noqa: BLE001 — one failed branch must not sink the brief
        text = ""
    return {"notes": [{"name": task["name"], "text": (text or "").strip()}]}


async def synthesize(state: LandscapeState) -> dict:
    order = {n: i for i, n in enumerate(state["contenders"])}
    blocks = [
        f"### {n['name']}\n{n['text'] or '(no usable findings)'}"
        for n in sorted(state["notes"], key=lambda n: order.get(n["name"], 0))
    ]
    table = await steps().llm(
        f"Subject: {state['subject']}\nDimensions: {', '.join(state['dimensions'])}\n\n"
        "Notes:\n\n" + "\n\n".join(blocks),
        system=_COMPARE_SYS,
    )
    return {"report": f"# {state['subject'].title()} — landscape\n\n{table}"}


def build() -> StateGraph:
    builder = StateGraph(LandscapeState)
    builder.add_node("scout", scout)
    builder.add_node("synthesize", synthesize)
    builder.add_conditional_edges(START, fan_out, ["scout"])
    builder.add_edge("scout", "synthesize")
    builder.add_edge("synthesize", END)
    return builder


def register(app):
    # A real subagent: isolated context, its own web_search.
    app.subagent(
        "scout",
        description="Research one option and report tight, sourced notes.",
        system_prompt=(
            "You research ONE option. Use web_search, then report 3–4 terse bullet "
            "findings about it — strengths, weaknesses, and what it's best for — each "
            "grounded in a source. No preamble. Do not invent facts."
        ),
        tools=["web_search"],
    )
    app.workflow(
        "landscape",
        graph=build(),
        input=Landscape,
        output_key="report",
        max_concurrency=5,
        description=(
            "Competitive-landscape brief: research each contender in its own parallel "
            "subagent, then synthesise one comparison table across the given dimensions."
        ),
    )
    return app


if __name__ == "__main__":
    app = make_app(system_prompt="When asked to compare options, run the `landscape` workflow.")
    register(app)
    app.run()
