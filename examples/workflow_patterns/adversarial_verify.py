"""
Pattern: ADVERSARIAL VERIFICATION  —  independent checkers try to REFUTE each claim.

Blog: "Adversarial verification"

Real job: fact-check a drafted answer before it ships. Decompose the draft into
atomic claims, then for each claim spawn N independent skeptic *subagents* — each
gathers its OWN evidence with web_search in an isolated context and tries to refute,
defaulting to REFUTED when unsure. A claim survives only if it isn't out-voted.

Independence is the whole game: each skeptic searches on its own and never sees the
others' findings — separate verdicts, not one context rationalising itself N times.

LangGraph shape: ``decompose`` → ``Send`` one ``skeptic`` task per (claim, vote) —
all in parallel, each checkpointed — → ``report`` tallies the votes per claim.

    /workflows run fact_check {"question": "Is SQLite a good prod database?",
        "answer": "SQLite supports unlimited concurrent writers; NASA uses it for telemetry.",
        "votes": 2}
"""

from __future__ import annotations

import operator
import re
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from examples.workflow_patterns._app import make_app, pick_label
from langclaw.workflows import steps

VERDICTS = ["refuted", "supported", "unverifiable"]
_URL_RE = re.compile(r"https?://\S+")
_VERDICT_RE = re.compile(r"verdict\s*:\s*([a-z]+)", re.IGNORECASE)

_EXTRACT_SYS = (
    "Extract the distinct, atomic, checkable factual claims from the answer. Skip "
    "opinions and hedges. Return at most 6 of the load-bearing claims."
)


def _verdict(reply: str) -> str:
    """Read the skeptic's verdict from its ``VERDICT:`` line, falling back to a scan.

    Reading the labelled line (not the whole reply) avoids a stray word in the prose
    flipping the verdict — e.g. "not clearly supported" must not count as *supported*.
    """
    m = _VERDICT_RE.search(reply)
    return pick_label(m.group(1) if m else reply, VERDICTS, default="unverifiable")


class Draft(BaseModel):
    question: str = Field(description="What the answer was responding to.")
    answer: str = Field(description="The drafted answer to verify, claim by claim.")
    votes: int = Field(default=2, ge=1, le=4, description="Independent skeptics per claim.")


class Claims(BaseModel):
    claims: list[str] = Field(description="Atomic, checkable factual claims.")


class FactCheckState(TypedDict, total=False):
    question: str
    answer: str
    votes: int
    claims: list[str]
    ballots: Annotated[list[dict], operator.add]
    report: str


class SkepticTask(TypedDict):
    claim: str


async def decompose(state: FactCheckState) -> dict:
    extracted = await steps().llm(state["answer"], schema=Claims, system=_EXTRACT_SYS)
    return {"claims": extracted.claims[:6]}


def fan_out(state: FactCheckState) -> list[Send] | str:
    if not state.get("claims"):
        return "report"
    return [
        Send("skeptic", {"claim": claim})
        for claim in state["claims"]
        for _ in range(state.get("votes", 2))
    ]


async def skeptic(task: SkepticTask) -> dict:
    reply = await steps().subagent("skeptic", f"CLAIM: {task['claim']}")
    text = reply if isinstance(reply, str) else str(reply)
    link = m.group(0).rstrip(".,)") if (m := _URL_RE.search(text)) else ""
    return {"ballots": [{"claim": task["claim"], "verdict": _verdict(text), "link": link}]}


def report(state: FactCheckState) -> dict:
    claims = state.get("claims") or []
    if not claims:
        return {"report": "# Fact-check\n\nNo checkable claims found."}
    rows, kept = [], 0
    for claim in claims:
        ballots = [b for b in state.get("ballots", []) if b["claim"] == claim]
        verdicts = [b["verdict"] for b in ballots]
        survived = verdicts.count("supported") > verdicts.count("refuted")  # ties lose
        kept += survived
        rows.append(f"{'✅' if survived else '❌'} {claim}  _( {'/'.join(verdicts)} )_")
        rows += [f"    ↳ {b['link']}" for b in ballots if b["link"]][:2]
    head = [
        f"# Fact-check — {kept}/{len(claims)} claims survived",
        "",
        f"**Question:** {state['question']}",
        "",
    ]
    return {"report": "\n".join(head + rows)}


def build() -> StateGraph:
    builder = StateGraph(FactCheckState)
    builder.add_node("decompose", decompose)
    builder.add_node("skeptic", skeptic)
    builder.add_node("report", report)
    builder.add_edge(START, "decompose")
    builder.add_conditional_edges("decompose", fan_out, ["skeptic", "report"])
    builder.add_edge("skeptic", "report")
    builder.add_edge("report", END)
    return builder


def register(app):
    # Verification: each skeptic does its OWN multi-step evidence-gathering → a subagent.
    app.subagent(
        "skeptic",
        description="Independently fact-check one claim with web search.",
        system_prompt=(
            "You are a skeptical fact-checker. Given a CLAIM, use web_search to find "
            "evidence, then TRY TO REFUTE it. If the evidence doesn't clearly support "
            "the claim, lean REFUTED. Reply on three lines:\n"
            "VERDICT: <refuted|supported|unverifiable>\n"
            "WHY: <one sentence>\n"
            "SOURCE: <a url you used, or none>"
        ),
        tools=["web_search"],
    )
    app.workflow(
        "fact_check",
        graph=build(),
        input=Draft,
        output_key="report",
        max_concurrency=6,
        description=(
            "Verify a drafted answer: decompose it into atomic claims, then for each "
            "claim spawn N independent skeptic subagents that gather their own evidence "
            "and vote refute/support. Returns a report of which claims survived."
        ),
    )
    return app


if __name__ == "__main__":
    app = make_app(system_prompt="When asked to fact-check or verify a claim, run `fact_check`.")
    register(app)
    app.run()
