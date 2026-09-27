"""
Pattern: CLASSIFY-AND-ACT  —  route work by type, then take a type-specific action.

Blog: "Classify-and-act" — https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code

Real job: triage an inbound support/issue report. One model call decides the
*kind* of ticket; a conditional edge then routes to a different node per kind — a
security report gets a live CVE search plus a severity assessment; a bug gets a
similar-issues search; a feature request gets scoped; a question gets answered from
the web. The classifier never does the work, and the handlers never re-classify.

The same workflow also exists as a file — ``triage.graph.json`` next to this module
(named ``triage_file``) — to show the no-code format the UI edits.

    /workflows run triage {"text": "the /login endpoint 500s when the password contains a +"}
"""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from examples.workflow_patterns._app import links, make_app
from langclaw.workflows import steps

_CLASSIFIER_SYS = "You are a precise support-ticket router."
_ASSESS_SYS = (
    "You are a senior on-call engineer. Given a ticket and its category, respond with a "
    "tight, skimmable assessment: severity (low/med/high), the single most likely cause, "
    "and the one next action. Markdown, <120 words."
)


class Ticket(BaseModel):
    text: str = Field(description="The raw inbound ticket / report text.")


class Routing(BaseModel):
    category: Literal["security", "bug", "feature_request", "question"]


class TriageState(TypedDict, total=False):
    text: str
    category: str
    report: str


async def classify(state: TriageState) -> dict:
    routing = await steps().llm(
        f"Classify this support ticket.\n\n{state['text']}",
        schema=Routing,
        system=_CLASSIFIER_SYS,
    )
    return {"category": routing.category}


def _header(state: TriageState) -> str:
    return f"# Triage — `{state['category']}`\n\n> {state['text'].strip()}\n\n"


async def security(state: TriageState) -> dict:
    hits = await steps().tool("web_search", query=f"{state['text']} CVE advisory", n=3)
    note = await steps().llm(f"category=security\n{state['text']}", system=_ASSESS_SYS)
    return {"report": f"{_header(state)}{note}\n\n## Related advisories\n{links(hits)}"}


async def bug(state: TriageState) -> dict:
    hits = await steps().tool("web_search", query=f"{state['text']} error fix github issue", n=3)
    note = await steps().llm(f"category=bug\n{state['text']}", system=_ASSESS_SYS)
    return {"report": f"{_header(state)}{note}\n\n## Possibly-related reports\n{links(hits)}"}


async def feature_request(state: TriageState) -> dict:
    note = await steps().llm(
        "category=feature_request. Instead of severity, give: user value, rough effort "
        f"(S/M/L), and one open question.\n{state['text']}",
        system=_ASSESS_SYS,
    )
    return {"report": f"{_header(state)}{note}"}


async def question(state: TriageState) -> dict:
    hits = await steps().tool("web_search", query=state["text"], n=3)
    note = await steps().llm(f"category=question\n{state['text']}", system=_ASSESS_SYS)
    return {"report": f"{_header(state)}{note}\n\n## Sources\n{links(hits)}"}


def build() -> StateGraph:
    builder = StateGraph(TriageState)
    builder.add_node("classify", classify)
    for handler in (security, bug, feature_request, question):
        builder.add_node(handler.__name__, handler)
        builder.add_edge(handler.__name__, END)
    builder.add_edge(START, "classify")
    # The route IS the category: each label names its handler node.
    builder.add_conditional_edges(
        "classify",
        lambda s: s["category"],
        ["security", "bug", "feature_request", "question"],
    )
    return builder


def register(app):
    app.workflow(
        "triage",
        graph=build(),
        input=Ticket,
        output_key="report",
        description=(
            "Triage an inbound ticket: classify it (security/bug/feature_request/"
            "question), then run the matching branch — CVE search, similar-issue "
            "search, scoping, or a web-grounded answer — and return a routed brief."
        ),
    )
    return app


if __name__ == "__main__":
    app = make_app(system_prompt="When the user reports an issue, run the `triage` workflow.")
    register(app)
    app.run()
