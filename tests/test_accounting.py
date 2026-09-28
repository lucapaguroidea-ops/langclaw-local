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
    from langclaw.accounting.journal import Journal

    Journal._ready.clear()  # the schema was dropped: the table cache is stale
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


@needs_pg
async def test_closing_a_period_needs_every_invoice_booked_then_locks_it(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    rows = [r for r in await scoped.store.search(limit=20) if r["status"] == "filed"]
    period = str(rows[0]["document_date"])[:7]
    in_period = [r for r in rows if str(r["document_date"]).startswith(period)]
    with tenant_scope(client):
        bad = await tools["accounting_period_report"].ainvoke({"period": "Sept"})
        assert "YYYY-MM" in bad["error"]
        refused = await tools["accounting_period_close"].ainvoke({"period": period})
        assert "no entry yet" in refused["error"] and len(refused["blockers"]) == len(in_period)

        for row in in_period:
            await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
            )
        report = await tools["accounting_period_report"].ainvoke({"period": period})
        assert report["blockers"] == [] and report["trial_balance"]["balanced"]
        vat = report["vat"]
        expected = sum(
            (1 if r["fields"]["direction"] == "out" else -1) * float(r["fields"]["total_vat"])
            for r in in_period
        )
        assert round(float(vat["payable"]) - float(vat["refundable"]), 2) == round(expected, 2)

        closed = await tools["accounting_period_close"].ainvoke(
            {"period": period, "closed_by": "ana"}
        )
        assert closed["closed"] == period
        saved, _ = await scoped.bucket.get(closed["report_key"])
        assert json.loads(saved)["period"] == period
        again = await tools["accounting_period_close"].ainvoke({"period": period})
        assert "already closed" in again["error"]

        # nothing can be posted into a closed month
        from langclaw.accounting.journal import Journal

        late = {**in_period[0], "bucket_key": "late.xml"}
        await scoped.store.save("late.xml", {k: late[k] for k in ("doc_type", "document_date",
                                "amount", "fields", "sender", "receiver")})  # fmt: skip
        posted = await tools["journal_post"].ainvoke(
            {"bucket_key": "late.xml", "proposal": _entry_for(late)}
        )
        assert "is closed" in posted["error"]
        assert await Journal(scoped.store).get("late.xml") is None


@needs_pg
async def test_a_month_does_not_close_without_its_expected_documents(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(
        id="acme",
        name="ACME",
        tax_id="RO12345678",
        profile={"vat_payer": True, "expected_documents": [{"doc_type": "bank_statement",
                                                           "label": "Extras BT"}]},
    )  # fmt: skip
    rows = [r for r in await scoped.store.search(limit=20) if r["status"] == "filed"]
    period = str(rows[0]["document_date"])[:7]
    with tenant_scope(client):
        for row in rows:
            if str(row["document_date"]).startswith(period):
                await tools["journal_post"].ainvoke(
                    {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
                )
        report = await tools["accounting_period_report"].ainvoke({"period": period})
        assert report["documents"]["missing"] == [{"doc_type": "bank_statement",
                                                   "label": "Extras BT"}]  # fmt: skip
        refused = await tools["accounting_period_close"].ainvoke({"period": period})
        assert "Extras BT" in refused["error"]

        await scoped.store.save(
            "bank/bt-09.pdf", {"doc_type": "bank_statement", "document_date": f"{period}-28"}
        )
        closed = await tools["accounting_period_close"].ainvoke({"period": period})
        assert closed["closed"] == period


@needs_pg
async def test_outlook_facts_come_from_the_books(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    profile = {"vat_payer": True, "vat_period": "monthly", "employees": 2}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile=profile)
    rows = [r for r in await scoped.store.search(limit=20) if r["status"] == "filed"]
    period = max(str(r["document_date"])[:7] for r in rows)
    with tenant_scope(client):
        for row in rows:
            await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
            )
        facts = await tools["accounting_outlook"].ainvoke({"period": period, "months": 3})
    assert {d["form"] for d in facts["deadlines"]} == {"D300", "D394", "D112"}
    assert facts["thresholds"] == []  # a VAT payer, not micro
    assert [m["period"] for m in facts["vat_trend"]["months"]][-1] == period
    assert len(facts["vat_trend"]["months"]) == 3
    sales = sum(
        float(r["fields"]["total_net"])
        for r in rows
        if r["fields"]["direction"] == "out" and str(r["document_date"])[:4] == period[:4]
    )
    assert round(float(facts["revenue_ytd"]), 2) == round(sales, 2)
    assert facts["unbooked_invoices"] == 0
    cash = facts["cash"]
    assert cash["bank_balance"] is None  # no statement imported
    unpaid_sales = sum(float(r["amount"]) for r in rows if r["fields"]["direction"] == "out")
    assert round(float(cash["receivables"]["total"]), 2) == round(unpaid_sales, 2)


def _mt940_paying(sale: dict[str, Any], purchase: dict[str, Any]) -> bytes:
    def amt(x: Any) -> str:
        return f"{float(x):.2f}".replace(".", ",")

    closing = 1000 + float(sale["amount"]) - float(purchase["amount"])
    return (
        ":20:ST1\n:25:RO49AAAA1B31007593840000\n:28C:1/1\n:60F:C260901RON1000,00\n"
        f":61:2609150915C{amt(sale['amount'])}NTRFNONREF//IN1\n"
        f":86:Incasare {sale['fields']['invoice_number']}\n"
        f":61:2609200920D{amt(purchase['amount'])}NTRFNONREF//OUT1\n"
        ":86:Plata servicii\n"
        f":62F:C260930RON{amt(closing)}\n"
    ).encode()


@needs_pg
async def test_bank_import_marks_invoices_paid_once(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678")
    rows = await scoped.store.search(doc_type="invoice", limit=20)
    sale = next(r for r in rows if r["fields"]["direction"] == "out")
    purchase = next(r for r in rows if r["fields"]["direction"] == "in")
    await scoped.bucket.put("bank/bt-2026-09.sta", _mt940_paying(sale, purchase))
    with tenant_scope(client):
        out = await tools["bank_import"].ainvoke({"key": "bank/bt-2026-09.sta"})
        assert out["problems"] == [] and out["imported"] == 2
        assert [m["bucket_key"] for m in out["paid"]] == [sale["bucket_key"]]
        assert (await scoped.store.get(sale["bucket_key"]))["fields"]["paid_on"] == "2026-09-15"
        statement = await scoped.store.get("bank/bt-2026-09.sta")
        assert statement["doc_type"] == "bank_statement" and statement["status"] == "filed"

        again = await tools["bank_import"].ainvoke({"key": "bank/bt-2026-09.sta"})
        assert again["imported"] == 0 and again["already_imported"] == 2

        open_ = (await tools["bank_movements"].ainvoke({}))["movements"]
        assert [m["reference"] for m in open_] == ["OUT1"]
        wrong = await tools["bank_confirm_match"].ainvoke(
            {"movement_key": open_[0]["key"], "bucket_key": sale["bucket_key"]}
        )
        assert "error" in wrong
        target = next((m["bucket_key"] for m in out["to_confirm"]), purchase["bucket_key"])
        ok = await tools["bank_confirm_match"].ainvoke(
            {"movement_key": open_[0]["key"], "bucket_key": target}
        )
        assert ok == {"paid": target, "movement": open_[0]["key"], "left": "0.00"}
        assert (await tools["bank_movements"].ainvoke({}))["movements"] == []
        facts = await tools["accounting_outlook"].ainvoke({"period": "2026-09"})
        closing = 1000 + float(sale["amount"]) - float(purchase["amount"])
        assert round(float(facts["cash"]["bank_balance"]), 2) == round(closing, 2)
        rec = facts["cash"]["receivables"]  # the paid sale drops out of the aging
        unpaid_sales = [r for r in rows if r["fields"]["direction"] == "out"
                        and r["bucket_key"] != sale["bucket_key"]]  # fmt: skip
        assert round(float(rec["total"]), 2) == round(
            sum(float(r["amount"]) for r in unpaid_sales), 2
        )


@needs_pg
async def test_the_overview_gathers_a_clients_month(acme) -> None:
    from langclaw.accounting.overview import accounting_overview
    from langclaw.tenants import Tenant

    services, scoped = acme
    profile = {"vat_payer": True, "expected_documents": ["bank_statement"]}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile=profile)
    rows = await scoped.store.search(limit=20)
    period = max(str(r["document_date"])[:7] for r in rows)
    view = await accounting_overview(services, client, period)
    assert view["period"] == view["report"]["period"] == view["outlook"]["period"] == period
    assert view["report"]["blockers"] and view["report"]["documents"]["missing"]
    assert "cash" in view["outlook"] and view["bank"]["movements"] == []
    assert "year_to_date" in view["results"] and view["partners"]["partners"] == []
    assert view["cash"]["period"] == period and "days" in view["cash"]
    assert view["reports"]["period"] == period and "files" in view["reports"]
    bad = await accounting_overview(services, client, "sept")
    assert "YYYY-MM" in bad["report"]["error"]


@needs_pg
async def test_the_monthly_loop_runs_end_to_end(acme) -> None:
    from datetime import date

    from langgraph.store.memory import InMemoryStore

    from langclaw.accounting.period import resolve_period
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.documents import build_document_tools
    from langclaw.tenants import Tenant, TenantRegistry, tenant_scope
    from langclaw.workflows.executor import build_toolset_executor
    from langclaw.workflows.graph import GraphWorkflowRunner, build_state_graph, parse_graph_spec
    from langclaw.workflows.registry import WorkflowSpec

    class Bus:
        def __init__(self) -> None:
            self.published: list[Any] = []

        async def publish(self, msg: Any) -> None:
            self.published.append(msg)

    services, scoped = acme
    bus = Bus()
    registry = TenantRegistry(InMemoryStore())
    await registry.save(
        Tenant(id="acme", name="ACME", tax_id="RO12345678", review_chat="telegram:-100111",
               profile={"vat_payer": True, "expected_documents": ["bank_statement"]})
    )  # fmt: skip
    tools = build_accounting_tools(services, bus=bus) + build_document_tools(services, bus=bus)
    real = build_toolset_executor(tools)
    prompts: list[str] = []

    async def executor(request):
        if request.kind == "llm":  # the only fake: the model
            prompts.append(request.payload["prompt"])
            return request.schema(status="2 invoices queued", summary="ok", items=[])
        return await real(request)

    path = (
        Path(__file__).resolve().parent.parent / "ui" / "templates" / "accounting_month.graph.json"
    )
    parsed = parse_graph_spec("accounting_month", json.loads(path.read_text()))
    spec = WorkflowSpec(name="accounting_month", graph=build_state_graph(parsed), graph_spec=parsed)
    runner = GraphWorkflowRunner(executor_provider=lambda: executor)
    runner.tenants = registry
    with tenant_scope(await registry.get("acme")):
        result = await runner.start(spec, {"period": ""}, run_id="month:1", tenant="acme")
    assert result.status == "waiting"  # a person approves the advice
    last_month = resolve_period("", today=date.today())
    assert f'"period": "{last_month}"' in prompts[0]  # empty period → last month
    queued = [m.metadata["workflow_input"] for m in bus.published]
    assert queued and all(m.metadata["tenant"] == "acme" for m in bus.published)
    done = await runner.resume(spec, "month:1", {"action": "approve", "by": "luca"})
    assert done.status == "completed"


def _mt940(*lines: tuple[str, float, str]) -> bytes:
    """A statement with (C|D, amount, description) movements, opening 1000."""
    body, total = "", 1000.0
    for i, (side, amount, desc) in enumerate(lines):
        total += amount if side == "C" else -amount
        body += f":61:260915091{i}{side}{amount:.2f}NTRFNONREF//P{i}\n:86:{desc}\n".replace(
            f"{amount:.2f}", f"{amount:.2f}".replace(".", ",")
        )
    return (
        ":20:ST2\n:25:RO49AAAA1B31007593840000\n:28C:2/1\n:60F:C260901RON1000,00\n"
        + body
        + f":62F:C260930RON{total:.2f}\n".replace(".", ",")
    ).encode()


@needs_pg
async def test_partial_payments_add_up_until_the_invoice_is_paid(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    number, gross = sale["fields"]["invoice_number"], float(sale["amount"])
    first = round(gross * 0.4, 2)
    await scoped.bucket.put("bank/p1.sta", _mt940(("C", first, f"avans {number}")))
    await scoped.bucket.put("bank/p2.sta", _mt940(("C", round(gross - first, 2), f"rest {number}")))
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        one = await tools["bank_import"].ainvoke({"key": "bank/p1.sta"})
        assert [m["kind"] for m in one["partial"]] == ["partial"]
        f = (await scoped.store.get(sale["bucket_key"]))["fields"]
        assert f["paid_amount"] == f"{first:.2f}" and not f.get("paid_on")
        facts = await tools["accounting_outlook"].ainvoke({"period": "2026-09"})
        two = await tools["bank_import"].ainvoke({"key": "bank/p2.sta"})
        assert [m["bucket_key"] for m in two["paid"]] == [sale["bucket_key"]]
        f = (await scoped.store.get(sale["bucket_key"]))["fields"]
        assert f["paid_on"] == "2026-09-15" and len(f["payments"]) == 2
        assert (await tools["bank_movements"].ainvoke({}))["movements"] == []
    assert facts["cash"]["receivables"]["total"] != "0.00"


@needs_pg
async def test_payments_and_fees_are_booked_in_the_journal(acme) -> None:
    from langclaw.accounting.journal import Journal
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    number, gross = sale["fields"]["invoice_number"], float(sale["amount"])
    await scoped.bucket.put(
        "bank/b1.sta", _mt940(("C", gross, f"incasare {number}"), ("D", 12.5, "Comision lunar"))
    )
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    with tenant_scope(client):
        await tools["journal_post"].ainvoke(
            {"bucket_key": sale["bucket_key"], "proposal": _entry_for(sale)}
        )
        out = await tools["bank_import"].ainvoke({"key": "bank/b1.sta"})
        assert out["booked_entries"] == 1 and out["not_booked"] == []
        assert [f["amount"] for f in out["fees"]] == ["-12.50"] and out["unmatched"] == 0
        report = await tools["accounting_period_report"].ainvoke({"period": "2026-09"})
    journal = Journal(scoped.store)
    tx_key = out["paid"][0]["key"]
    entry = await journal.get(f"bank/{tx_key}/{sale['bucket_key']}")
    assert [(x["account"], x["debit"], x["credit"]) for x in entry["lines"]] == [
        ("5121", round(gross, 2), 0.0), ("4111", 0.0, round(gross, 2))]  # fmt: skip
    accounts = {a["account"]: a for a in report["trial_balance"]["accounts"]}
    assert report["trial_balance"]["balanced"]
    assert accounts["4111"]["balance"] == "0.00"  # invoiced and collected
    assert accounts["627"]["balance"] == "12.50"
    assert float(accounts["5121"]["balance"]) == round(gross - 12.5, 2)


@needs_pg
async def test_cash_deposits_and_withdrawals_go_through_581(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    await scoped.bucket.put(
        "bank/c1.sta",
        _mt940(("C", 500.0, "Depunere numerar casierie"), ("D", 200.0, "Retragere numerar")),
    )
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        out = await tools["bank_import"].ainvoke({"key": "bank/c1.sta"})
        again = await tools["bank_import"].ainvoke({"key": "bank/c1.sta"})
        assert (await tools["bank_movements"].ainvoke({}))["movements"] == []
        report = await tools["accounting_period_report"].ainvoke({"period": "2026-09"})
    assert [c["kind"] for c in out["cash_transfers"]] == ["deposit", "withdrawal"]
    assert out["unmatched"] == 0 and out["not_booked"] == []
    assert again["imported"] == 0 and again["cash_transfers"] == []
    accounts = {a["account"]: a for a in report["trial_balance"]["accounts"]}
    assert accounts["5311"]["balance"] == "-300.00"
    assert accounts["5121"]["balance"] == "300.00"
    assert accounts["581"]["balance"] == "0.00"


@needs_pg
async def test_a_payment_in_a_closed_month_is_applied_but_not_booked(acme) -> None:
    from langclaw.accounting.journal import Journal
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    await Journal(scoped.store).close_period("2026-09", closed_by="ana")
    await scoped.bucket.put(
        "bank/b2.sta",
        _mt940(("C", float(sale["amount"]), f"incasare {sale['fields']['invoice_number']}")),
    )
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        out = await tools["bank_import"].ainvoke({"key": "bank/b2.sta"})
    assert out["paid"] and out["booked_entries"] == 0
    assert "closed" in out["not_booked"][0]["reason"]
    assert (await scoped.store.get(sale["bucket_key"]))["fields"]["paid_on"]


@needs_pg
async def test_closing_a_month_posts_the_vat_settlement(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.journal import Journal
    from langclaw.accounting.period import parse_period
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    rows = [r for r in await scoped.store.search(limit=20) if r["status"] == "filed"]
    period = max(str(r["document_date"])[:7] for r in rows)
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    with tenant_scope(client):
        for row in rows:  # every month up to the one we close
            await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
            )
        preview = await tools["accounting_period_report"].ainvoke({"period": period})
        assert preview["vat_settlement"]["lines"]
        closed = await tools["accounting_period_close"].ainvoke({"period": period})
    assert closed["closed"] == period and closed["vat_settlement"] == preview["vat_settlement"]
    journal = Journal(scoped.store)
    _, end = parse_period(period)
    assert await journal.balance_until(end, "4426") == Decimal("0.00")
    assert await journal.balance_until(end, "4427") == Decimal("0.00")
    net = await journal.balance_until(end, "4423") + await journal.balance_until(end, "4424")
    sales_vat = sum(Decimal(r["fields"]["total_vat"]) for r in rows
                    if r["fields"]["direction"] == "out")  # fmt: skip
    buy_vat = sum(Decimal(r["fields"]["total_vat"]) for r in rows
                  if r["fields"]["direction"] == "in")  # fmt: skip
    assert net == buy_vat - sales_vat  # 4423 credit (−) / 4424 debit (+)


@needs_pg
async def test_closing_a_month_posts_depreciation(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.journal import Journal
    from langclaw.accounting.period import parse_period
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    rows = [r for r in await scoped.store.search(limit=20) if r["status"] == "filed"]
    period = max(str(r["document_date"])[:7] for r in rows)
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        bad = await tools["assets_add"].ainvoke(
            {"name": "Stoc", "account": "371", "value": 10, "in_service": "2026-01-01",
             "life_months": 12}
        )  # fmt: skip
        assert "2xx" in bad["error"]
        added = await tools["assets_add"].ainvoke(
            {"name": "Laptop", "account": "2131", "value": 3600, "in_service": "2025-01-15",
             "life_months": 36}
        )  # fmt: skip
        assert added["asset"]["id"]
        listed = await tools["assets_list"].ainvoke({"period": period})
        assert listed["assets"][0]["this_month"] == "100.00"
        for row in rows:
            await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
            )
        closed = await tools["accounting_period_close"].ainvoke({"period": period})
    assert closed["depreciation"]["lines"][0] == {
        "account": "6811", "debit": "100.00", "credit": "0", "explanation": f"Amortizare {period}"
    }  # fmt: skip
    _, end = parse_period(period)
    assert await Journal(scoped.store).balance_until(end, "2813") == Decimal("-100.00")


@needs_pg
async def test_results_come_from_the_journal(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    rows = [r for r in await scoped.store.search(limit=20) if r["status"] == "filed"]
    period = max(str(r["document_date"])[:7] for r in rows)
    profile = {"vat_payer": True, "tax_regime": "micro"}
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678", profile=profile)):
        for row in rows:
            await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
            )
        res = await tools["accounting_results"].ainvoke({"period": period})
        facts = await tools["accounting_outlook"].ainvoke({"period": period})
    year = [r for r in rows if str(r["document_date"])[:4] == period[:4]]
    sales = sum(float(r["fields"]["total_net"]) for r in year if r["fields"]["direction"] == "out")
    costs = sum(float(r["fields"]["total_net"]) for r in year if r["fields"]["direction"] == "in")
    ytd = res["year_to_date"]
    assert round(float(ytd["revenue"]), 2) == round(sales, 2)
    assert round(float(ytd["expenses"]), 2) == round(costs, 2)
    assert res["tax_estimate"]["regime"] == "micro"
    assert float(res["tax_estimate"]["tax"]) == round(sales / 100, 2)
    assert facts["results_ytd"]["tax_estimate"] == res["tax_estimate"]


@needs_pg
async def test_partner_statement_and_balances(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    cui, gross = sale["fields"]["customer_cui"], round(float(sale["amount"]), 2)
    first = round(gross * 0.4, 2)
    await scoped.bucket.put(
        "bank/s1.sta", _mt940(("C", first, f"avans {sale['fields']['invoice_number']}"))
    )
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        await tools["journal_post"].ainvoke(
            {"bucket_key": sale["bucket_key"], "proposal": _entry_for(sale)}
        )
        await tools["bank_import"].ainvoke({"key": "bank/s1.sta"})
        st = await tools["partner_statement"].ainvoke({"partner_cui": cui})
        balances = await tools["partner_balances"].ainvoke({"day": "2026-12-31"})
        missing = await tools["partner_statement"].ainvoke({"partner_cui": ""})
    assert st["partner"]["cui"] == cui and st["opening"] == "0.00"
    assert [m["debit"] for m in st["movements"]] == [f"{gross:.2f}", "0.00"]
    assert st["movements"][-1]["balance"] == st["closing"] == f"{gross - first:.2f}"
    row = next(b for b in balances["partners"] if b["cui"] == cui)
    assert row["receivable"] == f"{gross - first:.2f}" and row["payable"] == "0.00"
    assert "partner_cui" in missing["error"]


@needs_pg
async def test_receivables_overdue_lists_unpaid_sales(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    sales = [r for r in await scoped.store.search(doc_type="invoice", limit=20)
             if r["fields"]["direction"] == "out" and r["fields"].get("due_date")]  # fmt: skip
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        out = await tools["receivables_overdue"].ainvoke({"day": "2027-12-31", "min_days": 1})
    listed = {i["bucket_key"] for c in out["customers"] for i in c["invoices"]}
    assert listed == {r["bucket_key"] for r in sales} and out["client"] == "ACME"


@needs_pg
async def test_filed_reminders_are_documents_and_invoice_history(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        overdue = await tools["receivables_overdue"].ainvoke({"day": "2027-12-31"})
        customer = overdue["customers"][0]
        reminder = {"partner": customer["partner"], "cui": customer["cui"],
                    "subject": "Reamintire plată", "body": "Vă rugăm..."}  # fmt: skip
        out = await tools["reminders_file"].ainvoke({"reminders": [reminder], "day": "2027-12-31"})
        again = await tools["receivables_overdue"].ainvoke({"day": "2027-12-31"})
    filed = out["filed"][0]
    assert filed["invoices"] == [i["bucket_key"] for i in customer["invoices"]]
    doc = await scoped.store.get(filed["document"])
    assert (
        doc["doc_type"] == "payment_reminder" and doc["fields"]["customer_cui"] == customer["cui"]
    )
    data, _ = await scoped.bucket.get(filed["document"])
    assert data.decode().startswith("Reamintire plată")
    inv = next(c for c in again["customers"] if c["cui"] == customer["cui"])["invoices"][0]
    assert inv["reminders_sent"] == 1 and inv["last_reminder"] == "2027-12-31"


@needs_pg
async def test_the_reminders_workflow_files_what_was_approved(acme) -> None:
    from langgraph.store.memory import InMemoryStore

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, TenantRegistry, tenant_scope
    from langclaw.workflows.executor import build_toolset_executor
    from langclaw.workflows.graph import GraphWorkflowRunner, build_state_graph, parse_graph_spec
    from langclaw.workflows.registry import WorkflowSpec

    services, scoped = acme
    registry = TenantRegistry(InMemoryStore())
    await registry.save(Tenant(id="acme", name="ACME", tax_id="RO12345678"))
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    await scoped.store.save(sale["bucket_key"], {"fields": {"due_date": "2026-01-01"}})
    real = build_toolset_executor(build_accounting_tools(services))
    seen: list[int] = []

    async def executor(request):
        if request.kind == "llm":  # the only fake: the model
            facts = json.loads(request.payload["prompt"].split("\n", 1)[1])
            seen.append(len(facts["customers"]))
            reminders = [{"partner": c["partner"], "cui": c["cui"], "subject": "Reamintire",
                          "body": "...", "total": c["outstanding"],
                          "invoices": [i["bucket_key"] for i in c["invoices"]]}
                         for c in facts["customers"]]  # fmt: skip
            return request.schema(reminders=reminders)
        return await real(request)

    path = Path(__file__).resolve().parent.parent / "ui/templates/payment_reminders.graph.json"
    parsed = parse_graph_spec("payment_reminders", json.loads(path.read_text()))
    spec = WorkflowSpec(
        name="payment_reminders", graph=build_state_graph(parsed), graph_spec=parsed
    )
    runner = GraphWorkflowRunner(executor_provider=lambda: executor)
    runner.tenants = registry
    with tenant_scope(await registry.get("acme")):
        started = await runner.start(spec, {"min_days": 0}, run_id="rem:1", tenant="acme")
        assert started.status == "waiting"
        done = await runner.resume(spec, "rem:1", {"action": "approve", "by": "luca"})
    assert done.status == "completed"
    reminders = await scoped.store.search(doc_type="payment_reminder", limit=20)
    assert seen[0] > 0 and len(reminders) == seen[0]
    assert (await scoped.store.get(sale["bucket_key"]))["fields"]["reminders"]


@needs_pg
async def test_filing_reminders_drafts_emails_when_a_mailer_is_given(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    sent: list[tuple[str, str, str]] = []

    async def mailer(to: str, subject: str, body: str) -> dict:
        sent.append((to, subject, body))
        return {"status": "drafted", "draft_id": f"d{len(sent)}"}

    tools = {t.name: t for t in build_accounting_tools(services, mailer=mailer)}
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        overdue = await tools["receivables_overdue"].ainvoke({"day": "2027-12-31"})
        reminders = [{"partner": c["partner"], "cui": c["cui"], "subject": "Reamintire",
                      "body": "Vă rugăm..."} for c in overdue["customers"]]  # fmt: skip
        reminders.append({"partner": "Fara email", "cui": "RO0", "subject": "x", "body": "y"})
        out = await tools["reminders_file"].ainvoke({"reminders": reminders, "day": "2027-12-31"})
    with_email = [c for c in overdue["customers"] if c["email"]]
    assert with_email and len(sent) == len(with_email)
    assert {s[0] for s in sent} == {c["email"] for c in with_email}
    assert out["filed"][-1]["draft"] == "no email address for this customer"
    assert all(f["draft"].startswith("d") for f in out["filed"] if f.get("to"))


@needs_pg
async def test_payables_batch_writes_a_csv_for_suppliers_due(acme) -> None:
    import csv
    import io

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    buys = [r for r in await scoped.store.search(doc_type="invoice", limit=20)
            if r["fields"]["direction"] == "in" and r["fields"].get("due_date")]  # fmt: skip
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        due = await tools["payables_due"].ainvoke({"day": "2027-12-31", "days": 0})
        out = await tools["payables_batch"].ainvoke({"day": "2027-12-31", "days": 0})
    listed = {k for s in due["suppliers"] for k in s["invoices"]}
    assert listed == {r["bucket_key"] for r in buys}
    data, _ = await scoped.bucket.get(out["key"])
    rows = list(csv.reader(io.StringIO(data.decode()), delimiter=";"))
    assert rows[0][:3] == ["beneficiary", "tax_id", "iban"] and len(rows) - 1 == out["payments"]
    with_iban = [s for s in due["suppliers"] if s["iban"]]
    assert out["payments"] == len(with_iban) and all(r[2] for r in rows[1:])
    assert (await scoped.store.get(out["key"]))["doc_type"] == "payment_batch"


@needs_pg
async def test_d394_figures_match_the_months_invoices(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    rows = await scoped.store.search(doc_type="invoice", limit=20)
    period = max(str(r["document_date"])[:7] for r in rows)
    month = [r for r in rows if str(r["document_date"]).startswith(period)]
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        out = await tools["accounting_d394"].ainvoke({"period": period})
    sales_vat = sum(Decimal(r["fields"]["total_vat"]) for r in month
                    if r["fields"]["direction"] == "out")  # fmt: skip
    got = sum(Decimal(r["vat"]) for r in out["rows"] if r["direction"] == "out")
    assert got == sales_vat and out["key"] == f"reports/{period}/d394.csv"
    data, _ = await scoped.bucket.get(out["key"])
    assert data.decode().splitlines()[0].startswith("direction;cui;partner")


@needs_pg
async def test_closing_december_posts_the_year_end_entry(acme) -> None:
    from datetime import date
    from decimal import Decimal

    from langclaw.accounting.journal import Journal
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    journal = Journal(scoped.store)

    def entry(debit: str, credit: str, amount: int) -> dict:
        return {"lines": [{"account": debit, "debit": amount, "credit": 0},
                          {"account": credit, "debit": 0, "credit": amount}]}  # fmt: skip

    def doc(key: str, day: str, direction: str) -> dict:
        return {"bucket_key": key, "document_date": day, "fields": {"direction": direction}}

    await journal.post(doc("manual/dec-sale", "2026-12-10", "out"), entry("4111", "704", 1000))
    await journal.post(doc("manual/dec-cost", "2026-12-12", "in"), entry("628", "401", 300))
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        preview = await tools["accounting_period_report"].ainvoke({"period": "2026-12"})
        closed = await tools["accounting_period_close"].ainvoke({"period": "2026-12"})
        results = await tools["accounting_results"].ainvoke({"period": "2026-12"})
    assert preview["year_end"] and closed["year_end"] == preview["year_end"]
    end = date(2026, 12, 31)
    assert await journal.balance_until(end, "704") == Decimal("0.00")
    assert await journal.balance_until(end, "628") == Decimal("0.00")
    assert await journal.balance_until(end, "121") == -(
        Decimal(results["year_to_date"]["result"])
    )  # a profit sits on the credit side of 121
    assert Decimal(results["month"]["revenue"]) == Decimal("1000.00")  # P&L ignores the close


@needs_pg
async def test_z_reports_are_booked_once_and_count_in_the_vat(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678",
                    profile={"vat_payer": True, "expected_documents": ["z_report"]})  # fmt: skip
    with tenant_scope(client):
        before = await tools["accounting_period_report"].ainvoke({"period": "2026-10"})
        out = await tools["cash_z_report"].ainvoke(
            {"day": "2026-10-05", "lines": [{"rate": 21, "gross": 1210}]}
        )
        twice = await tools["cash_z_report"].ainvoke(
            {"day": "2026-10-05", "lines": [{"rate": 21, "gross": 1210}]}
        )
        bad = await tools["cash_z_report"].ainvoke(
            {"day": "2026-10-06", "lines": [{"rate": 19, "gross": 119}]}
        )
        after = await tools["accounting_period_report"].ainvoke({"period": "2026-10"})
    assert [(x["account"], x["debit"], x["credit"]) for x in out["posted"]["lines"]] == [
        ("5311", 1210.0, 0.0), ("707", 0.0, 1000.0), ("4427", 0.0, 210.0)]  # fmt: skip
    assert "already posted" in twice["error"] and "19" in bad["error"]
    assert before["documents"]["missing"] and not after["documents"]["missing"]
    assert Decimal(after["vat"]["collected"]) - Decimal(before["vat"]["collected"]) == Decimal(
        "210.00"
    )


@needs_pg
async def test_the_cash_book_reads_5311_from_the_journal(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    await scoped.bucket.put("bank/c2.sta", _mt940(("C", 1500.0, "Depunere numerar")))
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"cash_limit": 1000})
    with tenant_scope(client):
        await tools["cash_z_report"].ainvoke(
            {"day": "2026-09-10", "lines": [{"rate": 21, "gross": 1210}]}
        )
        await tools["bank_import"].ainvoke({"key": "bank/c2.sta"})
        book = await tools["cash_book"].ainvoke({"period": "2026-09"})
        bad = await tools["cash_book"].ainvoke({"period": "2026-13"})
    assert book["opening"] == "0.00" and book["closing"] == "-290.00"
    assert [d["day"] for d in book["days"]] == ["2026-09-10", "2026-09-15"]
    kinds = sorted(p["problem"].split(":")[0] for p in book["problems"])
    assert kinds == ["Cash above the 1000.00 limit", "Cash negative"]
    assert "error" in bad


@needs_pg
async def test_invoices_paid_in_cash_go_through_5311(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    bill = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "in")  # fmt: skip
    gross = Decimal(str(bill["amount"])).quantize(Decimal("0.01"))
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678",
                    profile={"cash_payment_limit": 1})  # fmt: skip
    with tenant_scope(client):
        await tools["journal_post"].ainvoke(
            {"bucket_key": bill["bucket_key"], "proposal": _entry_for(bill)}
        )
        part = await tools["cash_pay_invoice"].ainvoke(
            {
                "bucket_key": bill["bucket_key"],
                "amount": "1.00",
                "day": "2026-09-20",
                "document": "DP 7",
            }  # fmt: skip
        )
        again = await tools["cash_pay_invoice"].ainvoke(
            {
                "bucket_key": bill["bucket_key"],
                "amount": "1.00",
                "day": "2026-09-20",
                "document": "DP 7",
            }  # fmt: skip
        )
        rest = await tools["cash_pay_invoice"].ainvoke(
            {"bucket_key": bill["bucket_key"], "day": "2026-09-21", "document": "DP 8"}
        )
        over = await tools["cash_pay_invoice"].ainvoke(
            {"bucket_key": bill["bucket_key"], "amount": "5", "day": "2026-09-22"}
        )
        book = await tools["cash_book"].ainvoke({"period": "2026-09"})
    assert [(x["account"], x["credit"]) for x in part["posted"]["lines"]][1] == ("5311", 1.0)
    assert part["posted"]["lines"][0]["account"].startswith("401")
    assert part["left"] == str(gross - 1) and part["warnings"] == []
    assert "already" in again["error"]
    assert rest["left"] == "0.00" and rest["paid"] == str(gross - 1)
    assert "limit" in rest["warnings"][0]
    assert "nothing left" in over["error"]
    f = (await scoped.store.get(bill["bucket_key"]))["fields"]
    assert f["paid_on"] == "2026-09-21" and len(f["payments"]) == 2
    assert book["closing"] == str(-gross) and book["problems"]


@needs_pg
async def test_cash_receipts_are_booked_once_and_count_as_deductible_vat(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    receipt = {"day": "2026-11-03", "amount": "121", "account": "6022", "vat_rate": 21,
               "document": "B 55", "description": "motorină"}  # fmt: skip
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    with tenant_scope(client):
        before = await tools["accounting_period_report"].ainvoke({"period": "2026-11"})
        out = await tools["cash_receipt"].ainvoke(receipt)
        twice = await tools["cash_receipt"].ainvoke(receipt)
        bad = await tools["cash_receipt"].ainvoke({**receipt, "account": "4111", "document": "x"})
        after = await tools["accounting_period_report"].ainvoke({"period": "2026-11"})
        book = await tools["cash_book"].ainvoke({"period": "2026-11"})
    assert [(x["account"], x["debit"], x["credit"]) for x in out["posted"]["lines"]] == [
        ("6022", 100.0, 0.0), ("4426", 21.0, 0.0), ("5311", 0.0, 121.0)]  # fmt: skip
    assert "already posted" in twice["error"] and "4111" in bad["error"]
    assert Decimal(after["vat"]["deductible"]) - Decimal(before["vat"]["deductible"]) == 21
    assert book["closing"] == "-121.00"


@needs_pg
async def test_employee_advances_are_settled_by_receipts_and_returned_cash(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": True})
    with tenant_scope(client):
        given = await tools["cash_advance"].ainvoke(
            {"day": "2026-11-02", "amount": "300", "employee": "Ana Pop", "document": "DP 1"}
        )
        await tools["cash_advance"].ainvoke(
            {"day": "2026-11-02", "amount": "100", "employee": "Ion Ene", "document": "DP 2"}
        )
        spent = await tools["cash_receipt"].ainvoke(
            {
                "day": "2026-11-04",
                "amount": "242",
                "account": "6022",
                "document": "B 9",
                "employee": "Ana Pop",
            }  # fmt: skip
        )
        open_mid = await tools["advances_open"].ainvoke({"day": "2026-11-05"})
        back = await tools["cash_advance"].ainvoke(
            {
                "day": "2026-11-06",
                "amount": "58",
                "employee": "Ana Pop",
                "document": "DI 3",
                "returned": True,
            }  # fmt: skip
        )
        too_much = await tools["cash_advance"].ainvoke(
            {"day": "2026-11-06", "amount": "500", "employee": "Ion Ene", "returned": True}
        )
        open_end = await tools["advances_open"].ainvoke({"day": "2026-11-30"})
        book = await tools["cash_book"].ainvoke({"period": "2026-11"})
    assert given["posted"]["lines"][0]["account"] == "542"
    assert spent["posted"]["lines"][-1]["account"] == "542"
    assert {a["employee"]: a["open"] for a in open_mid["advances"]} == {
        "Ana Pop": "58.00", "Ion Ene": "100.00"}  # fmt: skip
    assert back["open"] == "0.00" and "only 100.00" in too_much["error"]
    assert open_end["advances"] == [{"employee": "Ion Ene", "open": "100.00"}]
    assert open_end["total"] == "100.00"
    assert book["closing"] == "-342.00"


@needs_pg
async def test_the_month_report_shows_cash_and_close_refuses_negative_cash(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": False})
    with tenant_scope(client):
        await tools["cash_advance"].ainvoke(
            {"day": "2027-01-05", "amount": "200", "employee": "Ana Pop", "document": "DP 1"}
        )
        report = await tools["accounting_period_report"].ainvoke({"period": "2027-01"})
        refused = await tools["accounting_period_close"].ainvoke({"period": "2027-01"})
        await tools["cash_z_report"].ainvoke(
            {"day": "2027-01-04", "lines": [{"rate": 0, "gross": 500}]}
        )
        fixed = await tools["accounting_period_report"].ainvoke({"period": "2027-01"})
        closed = await tools["accounting_period_close"].ainvoke({"period": "2027-01"})
    assert report["cash"]["closing"] == "-200.00" and report["cash"]["problems"]
    assert report["cash"]["open_advances"] == [{"employee": "Ana Pop", "open": "200.00"}]
    assert [a["account"] for a in report["anomalies"]] == ["5311"]
    assert "negative" in refused["error"] and refused["problems"]
    assert fixed["cash"]["closing"] == "300.00" and fixed["cash"]["problems"] == []
    assert fixed["anomalies"] == []
    assert "error" not in closed


@needs_pg
async def test_the_cash_payment_limit_adds_up_a_partners_invoices_on_the_day(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    bill = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "in")  # fmt: skip
    copy = {k: v for k, v in bill.items() if k not in ("id", "bucket_key", "fields")}
    await scoped.store.save("inbox/second-bill.xml", {**copy, "fields": {
        **bill["fields"], "invoice_number": "SECOND-1", "paid_amount": "0"}})  # fmt: skip
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678",
                    profile={"cash_payment_limit": 100})  # fmt: skip
    pay = {"amount": "60", "day": "2026-09-20"}
    with tenant_scope(client):
        first = await tools["cash_pay_invoice"].ainvoke(
            {**pay, "bucket_key": bill["bucket_key"], "document": "DP 1"}
        )
        second = await tools["cash_pay_invoice"].ainvoke(
            {**pay, "bucket_key": "inbox/second-bill.xml", "document": "DP 2"}
        )
        next_day = await tools["cash_pay_invoice"].ainvoke(
            {**pay, "day": "2026-09-21", "bucket_key": "inbox/second-bill.xml", "document": "DP 3"}
        )
    assert first["warnings"] == [] and next_day["warnings"] == []
    assert "120.00" in second["warnings"][0] and bill["sender"] in second["warnings"][0]


@needs_pg
async def test_the_journal_register_lists_every_entry_or_only_non_invoice_ones(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    bill = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "in")  # fmt: skip
    period = str(bill["document_date"])[:7]
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        await tools["journal_post"].ainvoke(
            {"bucket_key": bill["bucket_key"], "proposal": _entry_for(bill)}
        )
        await tools["cash_z_report"].ainvoke(
            {"day": f"{period}-28", "lines": [{"rate": 21, "gross": 121}]}
        )
        full = await tools["accounting_journal_register"].ainvoke({"period": period})
        other = await tools["accounting_journal_register"].ainvoke(
            {"period": period, "without_invoices": True}
        )
        bad = await tools["accounting_journal_register"].ainvoke({"period": "2026-13"})
    assert full["entries"] == 2 and other["entries"] == 1
    assert full["balanced"] and full["debit"] == full["credit"]
    csv_text = (await scoped.bucket.get(other["key"]))[0].decode("utf-8")
    header, *rows = [r.split(";") for r in csv_text.strip().splitlines()]
    assert header == ["nr", "date", "document", "explanation", "account", "debit", "credit"]
    assert [r[4] for r in rows] == ["5311", "707", "4427"]
    assert rows[0][2] == f"cash/z/{period}-28" and rows[0][5] == "121.00"
    assert other["key"].endswith("registru-jurnal-other.csv") and "error" in bad


@needs_pg
async def test_the_account_ledger_reads_one_account_with_its_opening_balance(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        for day, gross in (("2026-08-30", 121), ("2026-09-03", 242)):
            await tools["cash_z_report"].ainvoke(
                {"day": day, "lines": [{"rate": 21, "gross": gross}]}
            )
        ledger = await tools["accounting_account_ledger"].ainvoke(
            {"account": "4427", "period": "2026-09"}
        )
        bad = await tools["accounting_account_ledger"].ainvoke(
            {"account": "x", "period": "2026-09"}
        )
    assert ledger["opening"] == "-21.00" and ledger["closing"] == "-63.00"
    assert [r["document"] for r in ledger["lines"]] == ["cash/z/2026-09-03"]
    assert ledger["lines"][0]["counterpart"] == "5311"
    assert ledger["key"].endswith("reports/2026-09/fisa-4427.csv") and "error" in bad


@needs_pg
async def test_the_trial_balance_tool_splits_opening_previous_and_month(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    z = tools["cash_z_report"]
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        await z.ainvoke({"day": "2025-12-15", "lines": [{"rate": 0, "gross": 100}]})
        await z.ainvoke({"day": "2026-02-10", "lines": [{"rate": 0, "gross": 40}]})
        await z.ainvoke({"day": "2026-03-10", "lines": [{"rate": 0, "gross": 5}]})
        sheet = await tools["accounting_trial_balance"].ainvoke({"period": "2026-03"})
    cash = next(r for r in sheet["accounts"] if r["account"] == "5311")
    assert (cash["opening_debit"], cash["previous_debit"], cash["month_debit"]) == (
        "100.00", "40.00", "5.00")  # fmt: skip
    assert cash["closing_debit"] == "145.00" and sheet["balanced"]
    assert sheet["key"].endswith("reports/2026-03/balanta.csv")


@needs_pg
async def test_opening_balances_are_posted_once_and_open_the_trial_balance(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    balances = {"5121": "1000", "1012": "-1000"}
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        out = await tools["accounting_opening_balances"].ainvoke(
            {"day": "2025-12-31", "balances": balances}
        )
        twice = await tools["accounting_opening_balances"].ainvoke(
            {"day": "2025-12-31", "balances": balances}
        )
        bad = await tools["accounting_opening_balances"].ainvoke(
            {"day": "2025-12-31", "balances": {"5121": "5"}}
        )
        sheet = await tools["accounting_trial_balance"].ainvoke({"period": "2026-01"})
        other = await tools["accounting_journal_register"].ainvoke(
            {"period": "2025-12", "without_invoices": True}
        )
    assert out["posted"]["bucket_key"] == "opening/2025-12-31"
    assert "already posted" in twice["error"] and "balance" in bad["error"]
    bank = next(r for r in sheet["accounts"] if r["account"] == "5121")
    assert bank["opening_debit"] == "1000.00" and sheet["balanced"]
    assert other["entries"] == 1


@needs_pg
async def test_closing_a_month_files_its_journal_register_and_trial_balance(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678", profile={"vat_payer": False})
    with tenant_scope(client):
        await tools["cash_z_report"].ainvoke(
            {"day": "2027-02-03", "lines": [{"rate": 0, "gross": 50}]}
        )
        closed = await tools["accounting_period_close"].ainvoke({"period": "2027-02"})
    assert closed["registers"] == {"journal": "reports/2027-02/registru-jurnal.csv",
                                   "trial_balance": "reports/2027-02/balanta.csv"}  # fmt: skip
    for key in closed["registers"].values():
        data, _ = await scoped.bucket.get(key)
        assert b"5311" in data
    with tenant_scope(client):
        files = await tools["accounting_reports"].ainvoke({"period": "2027-02"})
        empty = await tools["accounting_reports"].ainvoke({"period": "2027-03"})
    assert sorted(f["name"] for f in files["files"]) == [
        "balanta.csv", "close.json", "registru-jurnal.csv"]  # fmt: skip
    assert all(f["url"] and f["key"].startswith("reports/2027-02/") for f in files["files"])
    assert empty["files"] == []


@needs_pg
async def test_a_partner_who_buys_and_sells_can_be_offset(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    rows = await scoped.store.search(doc_type="invoice", limit=20)
    sale = next(r for r in rows if r["fields"]["direction"] == "out")
    bill = next(r for r in rows if r["fields"]["direction"] == "in")
    cui = sale["fields"]["customer_cui"]
    copy = {k: v for k, v in bill.items() if k not in ("id", "bucket_key", "fields")}
    await scoped.store.save("inbox/from-customer.xml", {**copy, "sender": sale["receiver"],
        "fields": {**bill["fields"], "supplier_cui": cui, "invoice_number": "BACK-1"}})  # fmt: skip
    bill2 = await scoped.store.get("inbox/from-customer.xml")
    small = min(Decimal(str(sale["amount"])), Decimal(str(bill2["amount"])))
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        for row in (sale, bill2):
            await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": _entry_for(row)}
            )
        out = await tools["partner_offset"].ainvoke({"partner_cui": cui, "day": "2026-09-30"})
        again = await tools["partner_offset"].ainvoke({"partner_cui": cui, "day": "2026-09-30"})
        nobody = await tools["partner_offset"].ainvoke({"partner_cui": "RO1", "day": "2026-09-30"})
        balances = await tools["partner_balances"].ainvoke({"day": "2026-09-30"})
    assert out["amount"] == str(small.quantize(Decimal("0.01")))
    assert [(x["account"], x["debit"] > 0) for x in out["posted"]["lines"]] == [
        ("401", True), ("4111", False)]  # fmt: skip
    assert "error" in again and "nothing to offset" in nobody["error"]
    mine = next(p for p in balances["partners"] if p["cui"] == cui)
    assert "0.00" in (mine["receivable"], mine["payable"])
    for key in (sale["bucket_key"], "inbox/from-customer.xml"):
        f = (await scoped.store.get(key))["fields"]
        assert any(p["tx"] == f"offset:2026-09-30:{cui}" for p in f["payments"])


@needs_pg
async def test_balance_confirmations_are_filed_and_drafted_per_partner(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    drafts = []

    async def mailer(to: str, subject: str, body: str) -> dict:
        drafts.append((to, subject, body))
        return {"draft_id": f"d{len(drafts)}"}

    tools = {t.name: t for t in build_accounting_tools(services, mailer=mailer)}
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    gross = f"{float(sale['amount']):.2f}"
    with tenant_scope(Tenant(id="acme", name="ACME SRL", tax_id="RO12345678")):
        await tools["journal_post"].ainvoke(
            {"bucket_key": sale["bucket_key"], "proposal": _entry_for(sale)}
        )
        out = await tools["partner_confirmations"].ainvoke({"day": "2026-12-31"})
    [item] = out["filed"]
    assert item["cui"] == sale["fields"]["customer_cui"] and item["receivable"] == gross
    assert item["document"] == f"confirmations/2026-12-31/{item['cui']}.txt"
    text = (await scoped.bucket.get(item["document"]))[0].decode("utf-8")
    assert gross in text and "31.12.2026" in text and "ACME SRL" in text
    assert drafts and drafts[0][0] == sale["fields"]["customer_email"]
    assert item["draft"] == "d1" and "Confirmare" in drafts[0][1]


@needs_pg
async def test_a_posted_entry_can_be_reversed_and_posted_again(acme) -> None:
    from langclaw.accounting.journal import Journal
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    bill = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "in")  # fmt: skip
    key, period = bill["bucket_key"], str(bill["document_date"])[:7]
    wrong = _entry_for(bill)
    wrong["lines"][0]["account"] = "6022"
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        await tools["journal_post"].ainvoke({"bucket_key": key, "proposal": wrong})
        no_reason = await tools["journal_reverse"].ainvoke({"bucket_key": key, "reason": ""})
        out = await tools["journal_reverse"].ainvoke(
            {"bucket_key": key, "day": f"{period}-28", "reason": "wrong expense account"}
        )
        report = await tools["accounting_period_report"].ainvoke({"period": period})
        again = await tools["journal_post"].ainvoke(
            {"bucket_key": key, "proposal": _entry_for(bill)}
        )
        missing = await tools["journal_reverse"].ainvoke({"bucket_key": "nope", "reason": "x"})
    assert "reason" in no_reason["error"] and "error" in missing
    assert out["reversal"]["bucket_key"] == f"reverse/1/{key}"
    assert out["original"] == f"{key}#reversed-1"
    assert key in [b["bucket_key"] for b in report["blockers"]]
    assert "error" not in again
    accounts = {a["account"]: a for a in report["trial_balance"]["accounts"]}
    assert accounts["6022"]["balance"] == "0.00"
    journal = Journal(scoped.store)
    assert (await journal.get(key))["lines"][0]["account"] == "628"
    assert (await scoped.store.get(key))["status"] == "posted"


@needs_pg
async def test_vat_on_collection_becomes_due_as_the_customer_pays(acme) -> None:
    from decimal import Decimal

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    number, gross = sale["fields"]["invoice_number"], float(sale["amount"])
    vat = Decimal(str(sale["fields"]["total_vat"]))
    entry = _entry_for(sale)
    entry["lines"][2]["account"] = "4428"
    half = round(gross / 2, 2)
    await scoped.bucket.put("bank/v1.sta", _mt940(("C", half, f"avans {number}")))
    client = Tenant(id="acme", name="ACME", tax_id="RO12345678",
                    profile={"vat_payer": True, "vat_on_collection": True})  # fmt: skip
    with tenant_scope(client):
        posted = await tools["journal_post"].ainvoke(
            {"bucket_key": sale["bucket_key"], "proposal": entry}
        )
        await tools["bank_import"].ainvoke({"key": "bank/v1.sta"})
        report = await tools["accounting_period_report"].ainvoke({"period": "2026-09"})
    assert "error" not in posted
    share = (vat * Decimal(str(half)) / Decimal(str(gross))).quantize(Decimal("0.01"))
    accounts = {a["account"]: a for a in report["trial_balance"]["accounts"]}
    assert Decimal(accounts["4427"]["balance"]) == -share
    assert Decimal(accounts["4428"]["balance"]) == -(vat - share)
    assert report["vat_settlement"] is not None
    assert report["vat"]["basis"] == "payments"
    assert sum(Decimal(r["vat"]) for r in report["vat"]["sales"]) == share
    assert Decimal(report["vat"]["collected"]) == share
    assert Decimal(report["vat"]["by_invoice"]["collected"]) == vat


@needs_pg
async def test_an_offset_moves_vat_on_collection_out_of_4428(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    rows = await scoped.store.search(doc_type="invoice", limit=20)
    sale = next(r for r in rows if r["fields"]["direction"] == "out")
    bill = next(r for r in rows if r["fields"]["direction"] == "in")
    cui = sale["fields"]["customer_cui"]
    copy = {k: v for k, v in bill.items() if k not in ("id", "bucket_key", "fields")}
    await scoped.store.save("inbox/from-customer.xml", {**copy, "sender": sale["receiver"],
        "fields": {**bill["fields"], "supplier_cui": cui, "invoice_number": "BACK-1"}})  # fmt: skip
    bill2 = await scoped.store.get("inbox/from-customer.xml")
    with tenant_scope(
        Tenant(
            id="acme",
            name="ACME",
            tax_id="RO12345678",
            profile={"vat_payer": True, "vat_on_collection": True},
        )
    ):
        for row in (sale, bill2):
            entry = _entry_for(row)
            for line in entry["lines"]:
                if line["account"] in ("4426", "4427"):
                    line["account"] = "4428"
            await tools["journal_post"].ainvoke(
                {"bucket_key": row["bucket_key"], "proposal": entry}
            )
        out = await tools["partner_offset"].ainvoke({"partner_cui": cui, "day": "2026-09-30"})
    accounts = [x["account"] for x in out["posted"]["lines"]]
    assert accounts[:2] == ["401", "4111"] and {"4427", "4426", "4428"} <= set(accounts)
    moved = sum(x["debit"] for x in out["posted"]["lines"] if x["account"] == "4428")
    assert moved > 0


@needs_pg
async def test_a_customer_advance_is_booked_on_419_and_applied_to_the_invoice(acme) -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.tenants import Tenant, tenant_scope

    services, scoped = acme
    tools = {t.name: t for t in build_accounting_tools(services)}
    sale = next(r for r in await scoped.store.search(doc_type="invoice", limit=20)
                if r["fields"]["direction"] == "out")  # fmt: skip
    cui = sale["fields"]["customer_cui"]
    await scoped.bucket.put("bank/a1.sta", _mt940(("C", 333.33, "avans comanda")))
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        imported = await tools["bank_import"].ainvoke({"key": "bank/a1.sta"})
        key = (await tools["bank_movements"].ainvoke({}))["movements"][0]["key"]
        booked = await tools["bank_book_advance"].ainvoke(
            {"movement_key": key, "partner_cui": cui, "partner_name": "Client"}
        )
        twice = await tools["bank_book_advance"].ainvoke({"movement_key": key, "partner_cui": cui})
        left = await tools["bank_movements"].ainvoke({})
        await tools["journal_post"].ainvoke(
            {"bucket_key": sale["bucket_key"], "proposal": _entry_for(sale)}
        )
        waiting = await tools["advances_partners"].ainvoke({"day": "2026-09-30"})
        report = await tools["accounting_period_report"].ainvoke({"period": "2026-09"})
        applied = await tools["advance_apply"].ainvoke(
            {"bucket_key": sale["bucket_key"], "day": "2026-09-30"}
        )
        done = await tools["advances_partners"].ainvoke({"day": "2026-09-30"})
        none_left = await tools["advance_apply"].ainvoke(
            {"bucket_key": sale["bucket_key"], "day": "2026-09-30"}
        )
    assert imported["unmatched"] == 1
    assert [(x["account"], x["credit"] > 0) for x in booked["posted"]["lines"]] == [
        ("5121", False), ("419", True)]  # fmt: skip
    assert "error" in twice and left["movements"] == []
    assert applied["amount"] == "333.33"
    assert [x["account"] for x in applied["posted"]["lines"]] == ["419", "4111"]
    assert "error" in none_left
    f = (await scoped.store.get(sale["bucket_key"]))["fields"]
    assert f["paid_amount"] == "333.33"
    assert waiting["advances"] == [{"cui": cui, "partner": "Client", "received": "333.33",
                                    "paid": "0.00"}]  # fmt: skip
    assert report["partner_advances"] == waiting["advances"]
    assert done["advances"] == []
