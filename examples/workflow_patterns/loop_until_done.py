"""
Pattern: LOOP-UNTIL-DONE  —  keep going until a real stop condition, not a fixed N.

Blog: "Loop-until-done" (a.k.a. loop-until-dry)

Real job: enumerate the edge cases / failure modes of something. You don't know up
front how many there are, so a fixed "give me 10" either pads with junk or stops
short. Instead, loop: each round asks for NEW items the run hasn't seen, dedupe
against everything accumulated, and stop when you hit the target, OR when N
consecutive rounds turn up nothing new (the well is dry), OR at a hard round cap.
The workflow returns an honest reason for *why* it stopped.

LangGraph shape: ``hunt`` loops back to itself through a conditional edge until a
stop condition sets ``reason``; each round is a checkpoint.

    /workflows run edge_hunt {"target": "a function that parses a user's birthday string",
        "target_count": 12, "patience": 2}
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from examples.workflow_patterns._app import make_app, norm
from langclaw.workflows import steps

_HUNTER_SYS = (
    "You find edge cases and failure modes. Given a TARGET and a list of cases ALREADY "
    "FOUND, propose up to 5 genuinely NEW ones not already covered. Terse. Return an "
    "empty list if you truly can't think of new ones."
)


class Hunt(BaseModel):
    target: str = Field(description="What to find edge cases / failure modes for.")
    target_count: int = Field(default=12, ge=1, le=40, description="Stop once this many found.")
    patience: int = Field(default=2, ge=1, le=5, description="Dry rounds before giving up.")
    max_rounds: int = Field(default=8, ge=1, le=20, description="Hard cap on rounds.")


class Cases(BaseModel):
    cases: list[str] = Field(description="New edge cases, terse, one per item.")


class HuntState(TypedDict, total=False):
    target: str
    target_count: int
    patience: int
    max_rounds: int
    found: list[str]
    dry: int
    rounds: int
    reason: str
    report: str


async def hunt(state: HuntState) -> dict:
    found = list(state.get("found", []))
    rounds = state.get("rounds", 0) + 1
    already = "\n".join(f"- {c}" for c in found) or "(nothing yet)"
    proposed = await steps().llm(
        f"TARGET: {state['target']}\n\nALREADY FOUND:\n{already}",
        schema=Cases,
        system=_HUNTER_SYS,
    )
    seen = {norm(c) for c in found}
    fresh = [c for c in proposed.cases[:5] if norm(c) and norm(c) not in seen]
    dry = 0 if fresh else state.get("dry", 0) + 1
    found += fresh

    reason = ""
    if len(found) >= state["target_count"]:
        reason, found = f"reached target of {state['target_count']}", found[: state["target_count"]]
    elif dry >= state["patience"]:
        reason = f"dried up after {dry} empty rounds"
    elif rounds >= state["max_rounds"]:
        reason = "hit round cap"
    return {"found": found, "dry": dry, "rounds": rounds, "reason": reason}


def report(state: HuntState) -> dict:
    found, rounds = state.get("found", []), state.get("rounds", 0)
    out = [
        f"# Edge cases — {len(found)} found",
        "",
        f"**Target:** {state['target']}",
        f"**Stopped:** {state['reason']} (after {rounds} round{'s' if rounds != 1 else ''})",
        "",
    ]
    out += [f"{i}. {c}" for i, c in enumerate(found, 1)]
    return {"report": "\n".join(out)}


def build() -> StateGraph:
    builder = StateGraph(HuntState)
    builder.add_node("hunt", hunt)
    builder.add_node("report", report)
    builder.add_edge(START, "hunt")
    builder.add_conditional_edges(
        "hunt", lambda s: "report" if s.get("reason") else "hunt", ["hunt", "report"]
    )
    builder.add_edge("report", END)
    return builder


def register(app):
    app.workflow(
        "edge_hunt",
        graph=build(),
        input=Hunt,
        output_key="report",
        description=(
            "Enumerate edge cases for a target by looping: each round proposes NEW cases, "
            "deduped against all found so far. Stops at the target count, after N dry "
            "rounds, or a round cap — and reports which."
        ),
    )
    return app


if __name__ == "__main__":
    app = make_app(
        system_prompt="When asked to brainstorm edge cases or risks exhaustively, run `edge_hunt`."
    )
    register(app)
    app.run()
