"""
WP-10 — monthly_close: one client-month, thread ``close:<cui>:<period>``.

lock_expected_set → diff → v2_gate (``v2_close`` interrupt) → outcome.

- **lock_expected_set** freezes the month's expected set (approved Jobs, via
  *load_expected*) as a hash; if a re-run finds a different set, that's a
  blocker (``lock mismatch``), not a silent refresh.
- **diff** buckets the witness against it with the saved explained rules and
  runs every control (:func:`~langclaw_acct.controls.period_diff`).
- **v2_gate** pauses for the accountant: ``{action: file|hold|patch_maps|reopen,
  explained_rule?}``. ``file`` on a material month is refused and asked again
  — no one, Jev included, can clear ``material``. Naming an ``explained_rule``
  (saved first through the rule store) re-runs the diff with it.
- ``file`` means the books support the declarations (V2); it is not an ANAF
  submission. Filing items and receipts are WP-12.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict

from langclaw_acct.catalog import Catalog
from langclaw_acct.controls import ControlInputs, buckets, may_file, period_diff
from langclaw_acct.rules import RuleStore
from langclaw_acct.sinks.eye import SagaEye
from langclaw_acct.types import ExpectedItem, PeriodDiff

GRAPH_ID = "monthly_close"
LoadExpected = Callable[[str, str], Awaitable[list[ExpectedItem]]]


class V2Close(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["file", "hold", "patch_maps", "reopen"]
    explained_rule: str | None = None
    by: str = ""
    """Stamped by the server relaying the answer."""


class CloseState(TypedDict, total=False):
    cui: str
    period: str
    lock_hash: str
    expected: list[dict[str, Any]]
    diff: dict[str, Any]
    controls: list[dict[str, Any]]
    refused: list[str]
    outcome: str
    decided_by: str


def expected_hash(items: list[ExpectedItem]) -> str:
    rows = sorted(i.model_dump_json() for i in items)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def close_graph(catalog: Catalog, eye: SagaEye, rules: RuleStore, load_expected: LoadExpected,
                profile: dict[str, Any] | None = None) -> StateGraph:  # fmt: skip
    """The uncompiled monthly_close graph."""
    catalog.hitl_kind("v2_close", graph_id=GRAPH_ID)

    async def lock_expected_set(state: CloseState) -> CloseState:
        items = await load_expected(state["cui"], state["period"])
        h = expected_hash(items)
        refused = list(state.get("refused", []))
        if state.get("lock_hash") and state["lock_hash"] != h:
            refused.append("lock mismatch: the expected set changed since the month was locked")
        return {"lock_hash": state.get("lock_hash") or h, "refused": refused,
                "expected": [i.model_dump() for i in items]}  # fmt: skip

    async def diff(state: CloseState) -> CloseState:
        expected = [ExpectedItem(**i) for i in state["expected"]]
        rows, holes = buckets(expected, eye.documents(state["cui"], state["period"]),
                              await rules.active())  # fmt: skip
        x = ControlInputs(cui=state["cui"], period=state["period"], eye=eye, rows=rows,
                          holes=holes, profile=profile or {})  # fmt: skip
        d, runs = period_diff(catalog, x, snapshot_id=state["lock_hash"][:16])
        blockers = [b for b in state.get("refused", []) if b.startswith("lock mismatch")]
        if blockers:
            d = d.model_copy(update={"material": True, "blockers": d.blockers + blockers})
        return {"diff": d.model_dump(), "controls": [r.model_dump() for r in runs]}

    async def v2_gate(state: CloseState) -> CloseState:
        d = state["diff"]
        answer = V2Close.model_validate(interrupt({
            "kind": "v2_close", "cui": state["cui"], "period": state["period"],
            "material": d["material"], "blockers": d["blockers"],
            "may_file": not d["material"] and d["hard_failures"] == 0,
            "refused": state.get("refused", []),
        }))  # fmt: skip
        refused = list(state.get("refused", []))
        if answer.explained_rule:
            if await rules.get(answer.explained_rule) is None:
                refused.append(f"no saved rule {answer.explained_rule!r}")
                return {"refused": refused, "outcome": "ask_again"}
            return {"outcome": "rediff"}
        if answer.action == "file":
            if not may_file(PeriodDiff(**d)):
                refused.append(f"file refused: {len(d['blockers'])} blocker(s)")
                return {"refused": refused, "outcome": "ask_again"}
        return {"outcome": answer.action, "decided_by": answer.by}

    def route(state: CloseState) -> str:
        return {"ask_again": "v2_gate", "rediff": "diff"}.get(state.get("outcome", ""), END)

    g = StateGraph(CloseState)
    g.add_node("lock_expected_set", lock_expected_set)
    g.add_node("diff", diff)
    g.add_node("v2_gate", v2_gate)
    g.add_edge(START, "lock_expected_set")
    g.add_edge("lock_expected_set", "diff")
    g.add_edge("diff", "v2_gate")
    g.add_conditional_edges("v2_gate", route, ["v2_gate", "diff", END])
    return g
