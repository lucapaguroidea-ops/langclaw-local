"""
WP-04 — ingest_source_doc: one Job from ``ingested`` to a person's decision.

extract → bind → checks → reconcile_pre → approve (``v3_approve`` interrupt)
→ mouth. Thread ``job:<job_id>``.

- **extract** reads the source through *read_source* (the bucket in
  production) and parses it deterministically (UBL); money and dates go into
  state as strings.
- **bind** confirms the Job's articol de cale is in the catalog. The articol
  came from triage with one candidate, so Jev isn't asked (AGENTS.md:
  ``|cands| = 1`` → skip Jev).
- **reconcile_pre** asks the witness (:class:`SagaEye`) whether the document is
  already in the books — same number and date, gross within 0.05 → the Job
  ends ``already_in_sink`` and nobody is asked. Without a witness for the
  month the review says ``sink_checked: false``.
- **approve** pauses for a person. Nothing with side effects runs before the
  interrupt; the resume payload is ``{decision: approve|reject|edit, edit?}``
  and anything else is refused.
- **mouth** is closed until Mouth-0 (00_LAW §8): an approved Job stays
  ``approved`` with ``mouth: closed``; no XML/DBF is written.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, model_validator

from langclaw.documents.efactura.ubl import UblError, parse_ubl
from langclaw_acct.catalog import Catalog
from langclaw_acct.jobs import JobStore
from langclaw_acct.sinks.eye import SagaEye
from langclaw_acct.types import JobRecord

ReadSource = Callable[[JobRecord], Awaitable[bytes]]
GRAPH_ID = "ingest_source_doc"
MATCH_TOLERANCE = Decimal("0.05")


class V3Approve(BaseModel):
    """Resume payload for ``v3_approve``: one decision, an edit only with ``edit``."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject", "edit"]
    edit: dict[str, str] | None = None
    reason: str = ""
    by: str = ""
    """Who answered — stamped by the server relaying the answer, not typed."""

    @model_validator(mode="after")
    def _xor(self) -> V3Approve:
        if (self.decision == "edit") != (self.edit is not None):
            raise ValueError("'edit' goes with decision 'edit', and only with it")
        return self


class IngestState(TypedDict, total=False):
    job_id: str
    document: dict[str, Any]
    problems: list[str]
    sink_checked: bool
    outcome: str
    decision: dict[str, Any]


def ingest_graph(catalog: Catalog, store: JobStore, read_source: ReadSource,
                 eye: SagaEye | None = None) -> StateGraph:  # fmt: skip
    """The uncompiled ingest_source_doc graph."""
    catalog.hitl_kind("v3_approve", graph_id=GRAPH_ID)  # fail at build, not at pause

    async def extract(state: IngestState) -> IngestState:
        job = await store.get(state["job_id"])
        try:
            inv = parse_ubl(await read_source(job))
        except UblError as exc:
            await store.move(job.job_id, "needs_human", note=f"can't read: {exc}")
            return {"outcome": "needs_human"}
        doc = {"number": inv.number, "date": inv.issue_date, "kind": inv.kind,
               "supplier_cui": inv.supplier.cui, "supplier": inv.supplier.name,
               "customer_cui": inv.customer.cui, "customer": inv.customer.name,
               "net": str(inv.total_net), "vat": str(inv.total_vat),
               "gross": str(inv.total_gross), "currency": inv.currency}  # fmt: skip
        await store.move(job.job_id, "extracted")
        return {"document": doc, "problems": inv.check()}

    async def bind(state: IngestState) -> IngestState:
        job = await store.get(state["job_id"])
        catalog.articol(job.articol_id)  # unknown → raises: the catalog is the law
        await store.move(job.job_id, "bound", note=job.articol_id)
        return {}

    async def reconcile_pre(state: IngestState) -> IngestState:
        job = await store.move(state["job_id"], "reconcile_pre")
        doc = state["document"]
        period = doc["date"][:7]
        if eye is None:
            return {"sink_checked": False}
        for sink in eye.documents(job.tenant_cui, period):
            if (
                sink.number == doc["number"]
                and sink.date == doc["date"]
                and abs(Decimal(sink.gross) - Decimal(doc["gross"])) <= MATCH_TOLERANCE
            ):
                await store.move(job.job_id, "already_in_sink", note=sink.saga_key)
                return {"sink_checked": True, "outcome": "already_in_sink"}
        return {"sink_checked": True}

    async def approve(state: IngestState) -> IngestState:
        answer = interrupt({"kind": "v3_approve", "job_id": state["job_id"],
                            "document": state["document"], "problems": state["problems"],
                            "sink_checked": state.get("sink_checked", False)})  # fmt: skip
        decision = V3Approve.model_validate(answer)
        by = decision.by
        if decision.decision == "reject":
            await store.move(state["job_id"], "rejected", note=decision.reason, by=by)
            return {"outcome": "rejected", "decision": decision.model_dump()}
        note = "edited: " + ", ".join(sorted(decision.edit)) if decision.edit else "approved"
        await store.move(state["job_id"], "approved", note=note, by=by)
        return {"decision": decision.model_dump()}

    async def mouth(state: IngestState) -> IngestState:
        return {"outcome": "approved_mouth_closed"}

    def after_extract(state: IngestState) -> str:
        return END if state.get("outcome") == "needs_human" else "bind"

    def after_pre(state: IngestState) -> str:
        return END if state.get("outcome") == "already_in_sink" else "approve"

    def after_approve(state: IngestState) -> str:
        return END if state.get("outcome") == "rejected" else "mouth"

    g = StateGraph(IngestState)
    for name, fn in [("extract", extract), ("bind", bind), ("reconcile_pre", reconcile_pre),
                     ("approve", approve), ("mouth", mouth)]:  # fmt: skip
        g.add_node(name, fn)
    g.add_edge(START, "extract")
    g.add_conditional_edges("extract", after_extract, ["bind", END])
    g.add_edge("bind", "reconcile_pre")
    g.add_conditional_edges("reconcile_pre", after_pre, ["approve", END])
    g.add_conditional_edges("approve", after_approve, ["mouth", END])
    g.add_edge("mouth", END)
    return g
