"""Accounting proposals (RO): VAT table, deterministic checks, journal, workflow."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from langclaw.accounting.checks import check_proposal
from langclaw.accounting.vat import allowed_vat_rates

D = Decimal


def test_vat_rates_depend_on_the_invoice_date() -> None:
    assert allowed_vat_rates(date(2025, 7, 31)) == {D(19), D(9), D(5), D(0)}
    assert {D(21), D(11), D(0)} <= allowed_vat_rates(date(2025, 8, 1))
    assert D(19) not in allowed_vat_rates(date(2026, 9, 15))
    assert D(5) not in allowed_vat_rates(date(2026, 9, 15))


INCOMING = {
    "direction": "in",
    "document_date": "2026-09-15",
    "total_net": "270.00",
    "total_vat": "56.70",
    "amount": 326.70,
    "vat_breakdown": [{"rate": "21.00", "taxable": "270.00", "vat": "56.70"}],
}
GOOD_IN = {
    "lines": [
        {"account": "605", "debit": 270.00, "credit": 0},
        {"account": "4426", "debit": 56.70, "credit": 0},
        {"account": "401", "debit": 0, "credit": 326.70},
    ]
}


def test_a_correct_supplier_invoice_entry_passes() -> None:
    assert check_proposal(GOOD_IN, INCOMING, profile={"vat_payer": True}) == []


def test_unbalanced_or_wrong_totals_are_caught() -> None:
    unbalanced = {"lines": [*GOOD_IN["lines"][:2], {"account": "401", "debit": 0, "credit": 300}]}
    problems = check_proposal(unbalanced, INCOMING, profile={"vat_payer": True})
    assert any("doesn't balance" in p for p in problems)
    assert any("supplier" in p and "326.70" in p for p in problems)

    wrong_vat = {
        "lines": [
            {"account": "605", "debit": 280.00, "credit": 0},
            {"account": "4426", "debit": 46.70, "credit": 0},
            {"account": "401", "debit": 0, "credit": 326.70},
        ]
    }
    assert any(
        "VAT" in p and "56.70" in p
        for p in check_proposal(wrong_vat, INCOMING, profile={"vat_payer": True})
    )


def test_rates_that_were_not_valid_on_the_date_are_caught() -> None:
    old_rate = {**INCOMING, "vat_breakdown": [{"rate": "19.00", "taxable": "270", "vat": "56.70"}]}
    problems = check_proposal(GOOD_IN, old_rate, profile={"vat_payer": True})
    assert any("19" in p and "2026-09-15" in p for p in problems)


def test_account_shapes_and_classes() -> None:
    bad = {
        "lines": [
            {"account": "7041", "debit": 270.00, "credit": 0},  # revenue on a purchase
            {"account": "44x6", "debit": 56.70, "credit": 0},
            {"account": "401", "debit": 0, "credit": 326.70},
        ]
    }
    problems = check_proposal(bad, INCOMING, profile={"vat_payer": True})
    assert any("7041" in p for p in problems) and any("44x6" in p for p in problems)
    both = {"lines": [{"account": "605", "debit": 1, "credit": 1}]}
    assert any("debit or a credit" in p for p in check_proposal(both, INCOMING, profile={}))
    assert any("no lines" in p for p in check_proposal({"lines": []}, INCOMING, profile={}))


def test_vat_regime_decides_the_vat_account() -> None:
    # not a VAT payer: no deductible VAT, the VAT is part of the cost
    assert any(
        "not a VAT payer" in p
        for p in check_proposal(GOOD_IN, INCOMING, profile={"vat_payer": False})
    )
    non_payer = {
        "lines": [
            {"account": "605", "debit": 326.70, "credit": 0},
            {"account": "401", "debit": 0, "credit": 326.70},
        ]
    }
    assert check_proposal(non_payer, INCOMING, profile={"vat_payer": False}) == []
    # VAT on collection: the VAT waits in 4428 until payment
    assert any(
        "4428" in p
        for p in check_proposal(
            GOOD_IN, INCOMING, profile={"vat_payer": True, "vat_on_collection": True}
        )
    )
    on_collection = {
        "lines": [
            {"account": "605", "debit": 270.00, "credit": 0},
            {"account": "4428", "debit": 56.70, "credit": 0},
            {"account": "401", "debit": 0, "credit": 326.70},
        ]
    }
    assert (
        check_proposal(
            on_collection, INCOMING, profile={"vat_payer": True, "vat_on_collection": True}
        )
        == []
    )


def test_a_sales_invoice_entry() -> None:
    outgoing = {**INCOMING, "direction": "out"}
    sale = {
        "lines": [
            {"account": "4111", "debit": 326.70, "credit": 0},
            {"account": "704", "debit": 0, "credit": 270.00},
            {"account": "4427", "debit": 0, "credit": 56.70},
        ]
    }
    assert check_proposal(sale, outgoing, profile={"vat_payer": True}) == []
    assert any(
        "customer" in p for p in check_proposal(GOOD_IN, outgoing, profile={"vat_payer": True})
    )


# -- the journal (real Postgres) and the tools -----------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

PG = os.environ.get("LANGCLAW_TEST_POSTGRES_DSN", "")
needs_pg = pytest.mark.skipif(not PG, reason="set LANGCLAW_TEST_POSTGRES_DSN")
boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture
async def acme():
    """A client's document services on real Postgres + a moto bucket, with 4 demo
    e-Factura invoices already imported."""
    from langclaw.config.schema import BucketConfig, DocumentsConfig
    from langclaw.documents import Bucket, DocumentServices, DocumentStore
    from langclaw.documents.efactura.spv import DemoSpvClient
    from langclaw.documents.efactura.sync import sync_efactura

    root = DocumentStore(PG)
    pool = await root._db()
    await pool.execute("DROP SCHEMA IF EXISTS tenant_acme CASCADE")
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs")
        services = DocumentServices(
            DocumentsConfig(efactura={"mode": "demo"}, accounting={"enabled": True}),
            bucket=Bucket(BucketConfig(name="docs"), client=s3),
            store=root,
            require_tenant=True,
        )
        scoped = services.scoped("acme")
        await sync_efactura(scoped, DemoSpvClient(invoices=4), cif="RO12345678", days=60)
        yield services, scoped
    await pool.execute("DROP SCHEMA IF EXISTS tenant_acme CASCADE")
    await root.close()


def _entry_for(row: dict[str, Any]) -> dict[str, Any]:
    """A correct entry for an imported demo invoice (what a good model would propose)."""
    f = row["fields"]
    gross = round(float(row["amount"]), 2)
    vat = float(f["total_vat"])
    net = round(gross - vat, 2)
    if f["direction"] == "in":
        lines = [
            {"account": "628", "debit": net, "credit": 0, "explanation": "services"},
            {"account": "4426", "debit": vat, "credit": 0},
            {"account": "401", "debit": 0, "credit": gross},
        ]
    else:
        lines = [
            {"account": "4111", "debit": gross, "credit": 0},
            {"account": "704", "debit": 0, "credit": net},
            {"account": "4427", "debit": 0, "credit": vat},
        ]
    return {
        "lines": lines,
        "legal_basis": "OMFP 1802/2014",
        "reasoning": "demo",
        "confidence": 0.95,
    }


@needs_pg
async def test_the_journal_only_takes_balanced_entries_once(acme) -> None:
    from langclaw.accounting.journal import Journal, JournalError

    _, scoped = acme
    journal = Journal(scoped.store)
    row = (await scoped.store.search(doc_type="invoice", limit=10))[0]
    entry = _entry_for(row)
    unbalanced = {"lines": [{**entry["lines"][0], "debit": 1}, *entry["lines"][1:]]}
    with pytest.raises(JournalError, match="balance"):
        await journal.post(row, unbalanced)
    posted = await journal.post(row, entry, approved_by="luca")
    assert posted["bucket_key"] == row["bucket_key"] and len(posted["lines"]) == 3
    with pytest.raises(JournalError, match="already posted"):
        await journal.post(row, entry)
    assert await journal.posted_keys([row["bucket_key"], "x"]) == {row["bucket_key"]}


@needs_pg
async def test_supplier_history_comes_from_posted_entries(acme) -> None:
    from langclaw.accounting.journal import Journal

    _, scoped = acme
    journal = Journal(scoped.store)
    incoming = [r for r in await scoped.store.search(limit=20) if r["fields"]["direction"] == "in"]
    for row in incoming:
        await journal.post(row, _entry_for(row))
    cui = incoming[0]["fields"]["supplier_cui"]
    history = await journal.supplier_history(cui)
    assert history[0]["account"] == "628" and history[0]["times"] >= 1


@needs_pg
async def test_accounting_tools_context_check_post(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    row = next(r for r in await scoped.store.search(limit=20) if r["fields"]["direction"] == "in")
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    with tenant_scope(client):
        ctx = await tools["accounting_context"].ainvoke({"bucket_key": row["bucket_key"]})
        assert ctx["invoice"]["direction"] == "in" and ctx["client"]["profile"]["vat_payer"]
        assert "21" in ctx["valid_vat_rates"] and ctx["posted"] is False

        good = _entry_for(row)
        assert await tools["accounting_check"].ainvoke(
            {"bucket_key": row["bucket_key"], "proposal": good}
        ) == {"ok": True, "problems": []}
        bad = {**good, "lines": good["lines"][:2]}
        checked = await tools["accounting_check"].ainvoke(
            {"bucket_key": row["bucket_key"], "proposal": bad}
        )
        assert checked["ok"] is False and checked["problems"]

        refused = await tools["journal_post"].ainvoke(
            {"bucket_key": row["bucket_key"], "proposal": bad}
        )
        assert "doesn't balance" in refused["error"]  # the model can't post a bad entry
        posted = await tools["journal_post"].ainvoke(
            {"bucket_key": row["bucket_key"], "proposal": good}
        )
        assert posted["posted"]["bucket_key"] == row["bucket_key"]
        assert (await scoped.store.get(row["bucket_key"]))["status"] == "posted"

    assert (
        "isn't linked to a client"
        in (await tools["accounting_context"].ainvoke({"bucket_key": row["bucket_key"]}))["error"]
    )


def _template() -> dict:
    path = (
        Path(__file__).resolve().parent.parent
        / "ui"
        / "templates"
        / "accounting_proposal.graph.json"
    )
    return json.loads(path.read_text())


def test_the_template_is_valid_against_the_real_tools() -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.config.schema import DocumentsConfig
    from langclaw.documents import DocumentServices
    from langclaw.workflows.graph import parse_graph_spec

    names = {t.name for t in build_accounting_tools(DocumentServices(DocumentsConfig()))}
    parse_graph_spec("accounting_proposal", _template(), available_tools=names)


@needs_pg
@pytest.mark.parametrize(
    ("confidence", "fix", "posts_without_review"),
    [(0.95, None, True), (0.6, None, False), (0.95, "unbalanced", False)],
)
async def test_the_workflow_end_to_end(acme, confidence, fix, posts_without_review) -> None:
    from langgraph.store.memory import InMemoryStore

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, TenantRegistry, tenant_scope
    from langclaw.workflows.executor import build_toolset_executor
    from langclaw.workflows.graph import GraphWorkflowRunner, build_state_graph, parse_graph_spec
    from langclaw.workflows.registry import WorkflowSpec

    services, scoped = acme
    registry = TenantRegistry(InMemoryStore())
    await registry.save(
        Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    )
    row = next(r for r in await scoped.store.search(limit=20) if r["fields"]["direction"] == "in")
    real = build_toolset_executor(build_accounting_tools(services))
    prompts: list[str] = []

    async def executor(request):
        if request.kind == "llm":  # the only fake: the model
            prompts.append(request.payload["prompt"])
            proposal = _entry_for(row) | {"confidence": confidence}
            if fix == "unbalanced":
                proposal["lines"] = proposal["lines"][:2]
            return request.schema(**proposal)
        return await real(request)

    parsed = parse_graph_spec("accounting_proposal", _template())
    spec = WorkflowSpec(
        name="accounting_proposal", graph=build_state_graph(parsed), graph_spec=parsed
    )
    runner = GraphWorkflowRunner(executor_provider=lambda: executor)
    runner.tenants = registry
    with tenant_scope(await registry.get("acme")):
        result = await runner.start(spec, {"key": row["bucket_key"]}, run_id="acc:1", tenant="acme")
    assert row["fields"]["supplier_cui"] in prompts[0]  # grounded in the invoice
    if posts_without_review:
        assert result.status == "completed"
    else:
        assert result.status == "waiting"  # doubt or failed checks → a person
        assert (await scoped.store.get(row["bucket_key"]))["status"] != "posted"
        edited = _entry_for(row)
        done = await runner.resume(
            spec, "acc:1", {"action": "edit", "data": {"propose": edited}, "by": "luca"}
        )
        assert done.status == "completed"
    assert (await scoped.store.get(row["bucket_key"]))["status"] == "posted"


@needs_pg
async def test_posted_invoices_export_to_saga_once(acme) -> None:
    import io
    import zipfile

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    rows = [r for r in await scoped.store.search(limit=20) if r["status"] == "filed"]
    with tenant_scope(client):
        empty = await tools["accounting_export"].ainvoke({})
        assert empty["exported"] == []
        for row in rows:
            posted = await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
            )
            assert "posted" in posted, posted
        out = await tools["accounting_export"].ainvoke({"target": "saga"})
        assert sorted(out["exported"]) == sorted(r["bucket_key"] for r in rows)
        assert out["key"].startswith("exports/saga/") and out["url"]
        data, _ = await scoped.bucket.get(out["key"])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert any(n.startswith("intrari/F_") for n in names)
        assert any(n.startswith("iesiri/F_12345678_") for n in names)
        assert (await scoped.store.get(rows[0]["bucket_key"]))["status"] == "exported"

        assert (await tools["accounting_export"].ainvoke({}))["exported"] == []  # once
        again = await tools["accounting_export"].ainvoke({"again": True})
        assert len(again["exported"]) == len(rows)
        nextup = await tools["accounting_export"].ainvoke({"target": "nextup"})
        assert "NextUp" in nextup["error"]
