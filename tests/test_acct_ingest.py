"""Poarta Primară: Job store (memory + Postgres) and ingest_source_doc (WP-04)."""

from __future__ import annotations

import os
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from langclaw.documents.efactura.samples import Party, make_invoice
from langclaw_acct.catalog import load_catalog
from langclaw_acct.ingest import V3Approve, ingest_graph
from langclaw_acct.jobs import JobError, MemoryJobStore, PgJobStore
from langclaw_acct.sinks import RegistruJurnalEye, read_registru_jurnal
from langclaw_acct.triage import DumpFile, emit_jobs, triage
from langclaw_acct.types import JobRecord

US = Party(name="Client Test SRL", cui="RO10000008")
SUPPLIER = Party(name="Furnizor SRL", cui="RO20000004")
DSN = os.environ.get("LANGCLAW_TEST_POSTGRES_DSN", "")


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def _xml(number: str = "A9") -> bytes:
    return make_invoice(number=number, issue_date="2026-09-10", supplier=SUPPLIER,
                        customer=US, lines=[("Servicii", 1, 100, 21)])  # fmt: skip


async def _job(catalog, store, data: bytes) -> str:
    packs = await emit_jobs(
        triage([DumpFile("a.xml", "text/xml", data)], tenant_cui=US.cui, catalog=catalog),
        tenant_cui=US.cui, catalog=catalog, store=store)  # fmt: skip
    return packs[0]["job_id"]


def _run(catalog, store, data: bytes, eye=None):
    async def read(_: JobRecord) -> bytes:
        return data

    return ingest_graph(catalog, store, read, eye).compile(checkpointer=MemorySaver())


async def test_a_job_pauses_for_a_person_then_is_approved_with_the_mouth_closed(catalog) -> None:
    store = MemoryJobStore()
    job_id = await _job(catalog, store, _xml())
    graph = _run(catalog, store, _xml())
    cfg = {"configurable": {"thread_id": f"job:{job_id}"}}
    paused = await graph.ainvoke({"job_id": job_id}, cfg)
    ask = paused["__interrupt__"][0].value
    assert ask["kind"] == "v3_approve" and ask["document"]["gross"] == "121.00"
    assert ask["sink_checked"] is False and ask["problems"] == []
    assert (await store.get(job_id)).status == "reconcile_pre"
    done = await graph.ainvoke(Command(resume={"decision": "approve", "by": "ana"}), cfg)
    assert done["outcome"] == "approved_mouth_closed"
    assert [e["status"] for e in await store.history(job_id)] == [
        "ingested",
        "extracted",
        "bound",
        "reconcile_pre",
        "approved",
    ]
    assert (await store.history(job_id))[-1]["by"] == "ana"


async def test_reject_ends_the_job(catalog) -> None:
    store = MemoryJobStore()
    job_id = await _job(catalog, store, _xml())
    graph, cfg = _run(catalog, store, _xml()), {"configurable": {"thread_id": "job:r"}}
    await graph.ainvoke({"job_id": job_id}, cfg)
    out = await graph.ainvoke(Command(resume={"decision": "reject", "reason": "dup"}), cfg)
    assert out["outcome"] == "rejected" and (await store.get(job_id)).status == "rejected"


def test_resume_payload_is_strict() -> None:
    with pytest.raises(ValidationError):
        V3Approve.model_validate({"decision": "approve", "approved": True})
    with pytest.raises(ValidationError, match="'edit' goes with"):
        V3Approve.model_validate({"decision": "approve", "edit": {"net": "1"}})
    with pytest.raises(ValidationError, match="'edit' goes with"):
        V3Approve.model_validate({"decision": "edit"})


async def test_a_document_already_in_the_books_asks_nobody(catalog) -> None:
    rj = ("data,nr_document,fel_document,explicatie,cont_debit,cont_credit,suma\n"
          "2026-09-10,A9,factura_intrare,Servicii,628,401,100\n"
          "2026-09-10,A9,factura_intrare,TVA,4426,401,21\n")  # fmt: skip
    eye = RegistruJurnalEye()
    eye.load("10000008", "2026-09", read_registru_jurnal("generic_csv", rj.encode()))
    store = MemoryJobStore()
    job_id = await _job(catalog, store, _xml())
    out = await _run(catalog, store, _xml(), eye).ainvoke(
        {"job_id": job_id}, {"configurable": {"thread_id": "job:s"}})  # fmt: skip
    assert out["outcome"] == "already_in_sink" and "__interrupt__" not in out
    assert (await store.get(job_id)).status == "already_in_sink"


async def test_an_unreadable_source_goes_to_a_person(catalog) -> None:
    store = MemoryJobStore()
    job_id = await _job(catalog, store, _xml())
    out = await _run(catalog, store, b"not xml").ainvoke(
        {"job_id": job_id}, {"configurable": {"thread_id": "job:u"}})  # fmt: skip
    assert out["outcome"] == "needs_human"


async def test_store_refuses_moves_the_machine_forbids() -> None:
    store = MemoryJobStore()
    job = JobRecord(job_id="j", tenant_cui="1", source_hash="h", articol_id="a",
                    schema_version="1")  # fmt: skip
    assert await store.add(job) and not await store.add(job.model_copy(update={"job_id": "k"}))
    with pytest.raises(JobError, match="ingested → approved"):
        await store.move("j", "approved")


@pytest.mark.skipif(not DSN, reason="LANGCLAW_TEST_POSTGRES_DSN not set")
async def test_postgres_store_is_unique_and_keeps_history() -> None:
    import asyncpg

    schema = f"acct_test_{uuid.uuid4().hex[:8]}"
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        store = PgJobStore(pool, schema)
        job = JobRecord(job_id="j1", tenant_cui="1", source_hash="h", articol_id="a",
                        schema_version="1")  # fmt: skip
        assert await store.add(job)
        assert not await store.add(job.model_copy(update={"job_id": "j2"}))
        await store.move("j1", "extracted", note="ok", by="ana")
        assert (await store.get("j1")).status == "extracted"
        with pytest.raises(JobError):
            await store.move("j1", "acked")
        assert [e["status"] for e in await store.history("j1")] == ["ingested", "extracted"]
    finally:
        async with pool.acquire() as con:
            await con.execute(f"DROP SCHEMA {schema} CASCADE")
        await pool.close()
