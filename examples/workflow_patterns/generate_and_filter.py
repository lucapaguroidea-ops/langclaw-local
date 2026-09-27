"""
Pattern: GENERATE-AND-FILTER  —  make many diverse candidates, keep the few that pass.

Blog: "Generate-and-filter"

Real job: a tagline studio. Generate N candidates from *deliberately different*
angles (bold, playful, technical, benefit-led, contrarian, minimalist) so the pool
is diverse rather than N variations of one idea, then score every candidate in
parallel against a rubric and return only the ones that clear the bar, ranked. The
generator and the judge are separate isolated calls — the judge never sees which
angle produced a line, so it can't play favourites.

LangGraph shape: two ``Send`` fan-outs in a row — ``write`` per angle, then
``judge`` per candidate — each collecting through an ``operator.add`` reducer.

    /workflows run tagline_studio {"product": "a durable workflow engine for AI agents",
        "audience": "Python developers", "n": 6, "keep": 3}
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

ANGLES = ["bold", "playful", "technical", "benefit-led", "contrarian", "minimalist"]

_WRITE_SYS = (
    "You are a senior copywriter. Given a product, an audience, and a stylistic ANGLE, "
    "write exactly ONE tagline (<=10 words). No quotes, no preamble, just the line."
)
_JUDGE_SYS = "You judge marketing taglines on clarity, memorability, and fit for the audience."


class StudioBrief(BaseModel):
    product: str = Field(description="What you're naming/positioning.")
    audience: str = Field(default="developers", description="Who it's for.")
    n: int = Field(default=6, ge=2, le=6, description="Candidates to generate.")
    keep: int = Field(default=3, ge=1, le=6, description="Top candidates to return.")
    bar: int = Field(default=6, ge=0, le=10, description="Minimum score to keep.")


class Score(BaseModel):
    score: int = Field(ge=0, le=10, description="0–10 rating.")
    why: str = Field(description="One-sentence justification.")


class StudioState(TypedDict, total=False):
    product: str
    audience: str
    n: int
    keep: int
    bar: int
    candidates: Annotated[list[dict], operator.add]
    scored: Annotated[list[dict], operator.add]
    report: str


def to_writers(state: StudioState) -> list[Send]:
    return [
        Send("write", {"product": state["product"], "audience": state["audience"], "angle": a})
        for a in ANGLES[: state.get("n", 6)]
    ]


async def write(task: dict) -> dict:
    line = await steps().llm(
        f"PRODUCT: {task['product']}\nAUDIENCE: {task['audience']}\nANGLE: {task['angle']}",
        system=_WRITE_SYS,
    )
    line = (line or "").strip()
    return {"candidates": [{"angle": task["angle"], "line": line}] if line else []}


def to_judges(state: StudioState) -> list[Send] | str:
    if not state.get("candidates"):
        return "report"
    return [Send("judge", {**c, "audience": state["audience"]}) for c in state["candidates"]]


async def judge(task: dict) -> dict:
    verdict = await steps().llm(
        f"AUDIENCE: {task['audience']}\nTAGLINE: {task['line']}", schema=Score, system=_JUDGE_SYS
    )
    return {"scored": [{**task, "score": verdict.score, "why": verdict.why}]}


def report(state: StudioState) -> dict:
    scored = sorted(state.get("scored", []), key=lambda s: s["score"], reverse=True)
    keep, bar = state.get("keep", 3), state.get("bar", 6)
    kept = [s for s in scored if s["score"] >= bar][:keep]
    note = ""
    if not kept:  # nothing cleared the bar — still return the best, honestly flagged
        kept = scored[:keep]
        note = f"_None cleared the bar ({bar}); showing the top {len(kept)} anyway._\n\n"
    out = [f"# Tagline studio — top {len(kept)} of {len(scored)}", "", note]
    for s in kept:
        out.append(f"**{s['score']}/10** · _{s['angle']}_ — {s['line']}")
        if s["why"]:
            out.append(f"    {s['why']}")
    return {"report": "\n".join(out)}


def build() -> StateGraph:
    builder = StateGraph(StudioState)
    builder.add_node("write", write)
    builder.add_node("gather", lambda s: {})  # join point after every writer finishes
    builder.add_node("judge", judge)
    builder.add_node("report", report)
    builder.add_conditional_edges(START, to_writers, ["write"])
    builder.add_edge("write", "gather")
    builder.add_conditional_edges("gather", to_judges, ["judge", "report"])
    builder.add_edge("judge", "report")
    builder.add_edge("report", END)
    return builder


def register(app):
    app.workflow(
        "tagline_studio",
        graph=build(),
        input=StudioBrief,
        output_key="report",
        max_concurrency=6,
        description=(
            "Generate N taglines from diverse angles, score each against a rubric in "
            "parallel, and return the top `keep` that clear the bar, ranked."
        ),
    )
    return app


if __name__ == "__main__":
    app = make_app(
        system_prompt="When asked for taglines or names, run the `tagline_studio` workflow."
    )
    register(app)
    app.run()
