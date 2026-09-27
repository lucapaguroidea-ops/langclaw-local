"""
Pattern: TOURNAMENT  —  rank by pairwise duels when absolute scoring is unreliable.

Blog: "Tournament" (pairwise comparative judgment)

Real job: prioritize a backlog. Asking a model to score ten items 1–10 in the
abstract is noisy; asking "which of these two better satisfies X" is far more
stable. So run a single-elimination bracket: pair items up, a referee judges each
duel (all duels in a round run in parallel), winners advance, repeat until one
champion remains. Odd rounds get a bye.

LangGraph shape: a loop — ``round`` plays one round, and a conditional edge sends it
back to ``round`` until one item remains. Every round is its own checkpoint, so a
crash mid-bracket resumes at the round it was on.

    /workflows run prioritize {"criterion": "impact per engineering-week",
        "items": ["SSO login", "dark mode", "audit log export", "faster cold start",
                  "Slack notifications", "CSV import"]}
"""

from __future__ import annotations

import asyncio
from typing import Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from examples.workflow_patterns._app import make_app
from langclaw.workflows import steps

_REFEREE_SYS = (
    "You are an impartial referee. Given a CRITERION and two options A and B, decide "
    "which one better satisfies the criterion."
)


class Bracket(BaseModel):
    items: list[str] = Field(description="The things to rank (2–16).")
    criterion: str = Field(description="What 'better' means in each duel.")


class Duel(BaseModel):
    winner: Literal["A", "B"]
    why: str = Field(description="One-sentence justification.")


class BracketState(TypedDict, total=False):
    items: list[str]
    criterion: str
    remaining: list[str]
    rounds: list[str]
    report: str


def seed(state: BracketState) -> dict:
    return {"remaining": [x.strip() for x in state["items"] if x and x.strip()], "rounds": []}


async def _duel(criterion: str, a: str, b: str | None) -> str:
    if b is None:  # bye — advances for free
        return a
    verdict = await steps().llm(
        f"CRITERION: {criterion}\nA: {a}\nB: {b}", schema=Duel, system=_REFEREE_SYS
    )
    return b if verdict.winner == "B" else a


async def play_round(state: BracketState) -> dict:
    current = state["remaining"]
    pairs = [
        (current[i], current[i + 1] if i + 1 < len(current) else None)
        for i in range(0, len(current), 2)
    ]
    winners = await asyncio.gather(*(_duel(state["criterion"], a, b) for a, b in pairs))
    lines = [
        f"- {a} _(bye)_" if b is None else f"- {a} vs {b} → **{w}**"
        for (a, b), w in zip(pairs, winners, strict=False)
    ]
    n = len(state.get("rounds", [])) + 1
    return {
        "remaining": list(winners),
        "rounds": [*state["rounds"], f"### Round {n}\n" + "\n".join(lines)],
    }


def report(state: BracketState) -> dict:
    remaining = state.get("remaining", [])
    if len(state.get("rounds", [])) == 0 and len(remaining) < 2:
        return {"report": f"# Priority\n\nNeed at least two items; got {len(remaining)}."}
    head = [
        f"# Priority by duel — 🏆 **{remaining[0]}**",
        "",
        f"_Criterion: {state['criterion']}_",
        "",
    ]
    return {"report": "\n".join(head + state["rounds"])}


def next_step(state: BracketState) -> str:
    return "round" if len(state.get("remaining", [])) > 1 else "report"


def build() -> StateGraph:
    builder = StateGraph(BracketState)
    builder.add_node("seed", seed)
    builder.add_node("round", play_round)
    builder.add_node("report", report)
    builder.add_edge(START, "seed")
    builder.add_conditional_edges("seed", next_step, ["round", "report"])
    builder.add_conditional_edges("round", next_step, ["round", "report"])
    builder.add_edge("report", END)
    return builder


def register(app):
    app.workflow(
        "prioritize",
        graph=build(),
        input=Bracket,
        output_key="report",
        description=(
            "Rank a list by single-elimination pairwise duels: a referee judges each "
            "pair (rounds run in parallel), winners advance until one champion remains. "
            "More robust than absolute 1–10 scoring."
        ),
    )
    return app


if __name__ == "__main__":
    app = make_app(system_prompt="When asked to rank or prioritize a list, run `prioritize`.")
    register(app)
    app.run()
