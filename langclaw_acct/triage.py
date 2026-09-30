"""
WP-02 — folder_triage: a shoebox dump in, Packs and Jobs out.

sniff → pair → identity → emit, per ARCHITECTURE.md §2:

- **sniff**: e-Factura XML is parsed deterministically (``ro_efactura_ubl``);
  anything else goes to the *classifier* (Document AI + Jev later), which by
  default doesn't know (``unknown`` → HITL ``define_class``).
- **pair**: a PDF whose name carries the invoice number, or the XML's file
  stem, is that invoice's visual — not a document of its own.
- **identity**: the tenant's CUI must be the supplier (outbound) or the
  customer (inbound); otherwise HITL ``which_cui``.
- **emit**: the pack's own json-logic ``emit`` rule decides; a PDF RO without
  UBL (``ro_efactura_pdf``) never emits. Jobs are unique on
  ``(tenant_cui, source_hash)``: the same file twice is one Job.

Files from a client's address are sniffed but held until a person approves
(:attr:`Admission.may_emit`). The graph runs on thread ``batch:<id>``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from langclaw.documents.efactura.ubl import UblError, parse_ubl
from langclaw_acct.catalog import Catalog
from langclaw_acct.jsonlogic import check
from langclaw_acct.types import JobRecord


def source_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_cui(cui: str) -> str:
    return re.sub(r"[^0-9]", "", cui or "")


@dataclass(slots=True)
class DumpFile:
    name: str
    content_type: str
    data: bytes


#: (file) → source_doc_id. Document AI + Jev go here; default: don't know.
Classifier = Callable[[DumpFile], str]


def unknown_classifier(_: DumpFile) -> str:
    return "unknown"


class JobStore(Protocol):
    def add(self, job: JobRecord) -> bool:
        """Store *job*; False when ``(tenant_cui, source_hash)`` is already there."""
        ...


@dataclass
class MemoryJobStore:
    jobs: dict[tuple[str, str], JobRecord] = field(default_factory=dict)

    def add(self, job: JobRecord) -> bool:
        key = (job.tenant_cui, job.source_hash)
        if key in self.jobs:
            return False
        self.jobs[key] = job
        return True


def _sniff(f: DumpFile, classify: Classifier) -> dict[str, Any]:
    pack: dict[str, Any] = {"file": f.name, "source_hash": source_hash(f.data)}
    looks_xml = f.name.lower().endswith(".xml") or "xml" in f.content_type
    if looks_xml:
        try:
            inv = parse_ubl(f.data)
        except UblError:
            pass
        else:
            pack.update(source_doc_id="ro_efactura_ubl", number=inv.number,
                        date=inv.issue_date, credit_note=inv.kind == "credit_note",
                        supplier=norm_cui(inv.supplier.cui), customer=norm_cui(inv.customer.cui),
                        gross=str(inv.total_gross), problems=inv.check())  # fmt: skip
            return pack
    pack["source_doc_id"] = classify(f)
    return pack


def _paired(pdf: dict[str, Any], ubls: list[dict[str, Any]]) -> dict[str, Any] | None:
    stem = PurePath(pdf["file"]).stem.lower()
    for u in ubls:
        if stem == PurePath(u["file"]).stem.lower():
            return u
        number = re.sub(r"\W", "", u.get("number", "")).lower()
        if number and number in re.sub(r"\W", "", stem):
            return u
    return None


def triage(files: list[DumpFile], *, tenant_cui: str, catalog: Catalog,
           classify: Classifier = unknown_classifier) -> list[dict[str, Any]]:  # fmt: skip
    """Sniff, pair and gate every file; returns one pack per file with
    ``outcome`` ∈ emit / visual / duplicate_in_batch / hitl and a ``reason``."""
    tenant = norm_cui(tenant_cui)
    packs = [_sniff(f, classify) for f in files]
    seen: set[str] = set()
    ubls = [p for p in packs if p["source_doc_id"] == "ro_efactura_ubl"]
    for p in packs:
        if p["source_hash"] in seen:
            p.update(outcome="duplicate_in_batch", reason="same file twice in this dump")
            continue
        seen.add(p["source_hash"])
        sdid = p["source_doc_id"]
        if sdid != "ro_efactura_ubl" and p["file"].lower().endswith(".pdf"):
            if (u := _paired(p, ubls)) is not None:
                why = "PDF of an e-Factura in this dump; the XML is the document"
                p.update(outcome="visual", pairs_with=u["file"], reason=why)
                continue
        row = catalog.rows["ArticoleSourceDoc"].get(sdid)
        if row is None or sdid == "unknown":
            p.update(outcome="hitl", hitl="define_class", reason="not recognised yet")
            continue
        identity_ok, role = True, None
        if sdid == "ro_efactura_ubl":
            role = ("outbound" if p["supplier"] == tenant
                    else "inbound" if p["customer"] == tenant else None)  # fmt: skip
            identity_ok = role is not None
        ctx = {"source_doc": row, "source_doc_id": sdid,
               "fiscal_class": row.get("fiscal_class"), "identity_ok": identity_ok}  # fmt: skip
        if sdid == "ro_efactura_ubl" and not identity_ok:
            p.update(outcome="hitl", hitl="which_cui",
                     reason=f"neither supplier nor customer is {tenant_cui}")  # fmt: skip
        elif not check("emit", ctx):
            p.update(outcome="hitl", hitl="define_class",
                     reason=f"{sdid} is not a primary document on its own")  # fmt: skip
        elif sdid == "ro_efactura_ubl":
            storno = p["credit_note"]
            p.update(outcome="emit", role=role, articol_id=(
                f"storno_{'iesire' if role == 'outbound' else 'intrare'}" if storno
                else f"ro_efactura_{role}"))  # fmt: skip
        else:
            p.update(outcome="hitl", hitl="define_articol",
                     reason=f"no walk wired for {sdid} yet")  # fmt: skip
    return packs


def emit_jobs(packs: list[dict[str, Any]], *, tenant_cui: str, catalog: Catalog,
              store: JobStore, may_emit: bool = True) -> list[dict[str, Any]]:  # fmt: skip
    """Mint a Job per ``emit`` pack. With *may_emit* False (client mail) they
    wait for a person (``outcome = awaiting_approval``)."""
    for p in packs:
        if p.get("outcome") != "emit":
            continue
        if not may_emit:
            p.update(outcome="awaiting_approval", reason="from the client's address")
            continue
        articol = catalog.articol(p["articol_id"])
        job = JobRecord(job_id=f"{norm_cui(tenant_cui)}:{p['source_hash'][:16]}",
                        tenant_cui=norm_cui(tenant_cui), source_hash=p["source_hash"],
                        articol_id=p["articol_id"],
                        schema_version=str(articol.get("schema_version", "1")))  # fmt: skip
        if store.add(job):
            p["job_id"] = job.job_id
        else:
            p.update(outcome="already_ingested", reason="this file was already a Job")
    return packs


class BatchState(TypedDict, total=False):
    batch_id: str
    tenant_cui: str
    may_emit: bool
    files: list[dict[str, Any]]
    packs: list[dict[str, Any]]


def folder_triage_graph(catalog: Catalog, store: JobStore,
                        classify: Classifier = unknown_classifier) -> StateGraph:  # fmt: skip
    """The uncompiled folder_triage graph (thread ``batch:<batch_id>``)."""

    def sniff(state: BatchState) -> BatchState:
        files = [DumpFile(**f) for f in state["files"]]
        return {"packs": triage(files, tenant_cui=state["tenant_cui"],
                                catalog=catalog, classify=classify)}  # fmt: skip

    def emit(state: BatchState) -> BatchState:
        return {"packs": emit_jobs(state["packs"], tenant_cui=state["tenant_cui"],
                                   catalog=catalog, store=store,
                                   may_emit=state.get("may_emit", True))}  # fmt: skip

    g = StateGraph(BatchState)
    g.add_node("sniff", sniff)
    g.add_node("emit", emit)
    g.add_edge(START, "sniff")
    g.add_edge("sniff", "emit")
    g.add_edge("emit", END)
    return g
