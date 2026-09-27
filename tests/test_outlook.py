"""Forward-looking facts (RO): deadlines, thresholds, VAT trend, the advice template."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from langclaw.accounting.outlook import deadlines, thresholds, trend

D = Decimal


def test_deadlines_follow_the_clients_obligations() -> None:
    vat_monthly = {"vat_payer": True, "vat_period": "monthly", "employees": 3}
    due = {d["form"]: d["due"] for d in deadlines("2026-09", vat_monthly)}
    assert due == {"D300": "2026-10-25", "D394": "2026-10-25", "D112": "2026-10-25"}

    quarterly = {"vat_payer": True, "vat_period": "quarterly", "tax_regime": "micro"}
    assert deadlines("2026-08", quarterly) == []  # mid-quarter: nothing yet
    due = {d["form"] for d in deadlines("2026-09", quarterly)}
    assert due == {"D300", "D394", "D100"}
    assert deadlines("2026-12", {"tax_regime": "micro"})[0]["due"] == "2027-01-25"


def test_thresholds_warn_before_they_are_crossed() -> None:
    non_payer = thresholds(D("300000"), year=2026, profile={"vat_payer": False})
    vat = next(t for t in non_payer if t["name"] == "vat_registration")
    assert vat["limit"] == D("395000") and vat["used_pct"] == D("75.9") and vat["warn"] is False
    close = thresholds(D("360000"), year=2026, profile={"vat_payer": False})
    assert next(t for t in close if t["name"] == "vat_registration")["warn"] is True
    assert thresholds(D("1"), year=2026, profile={"vat_payer": True}) == []

    micro = thresholds(D("450000"), year=2026, profile={"tax_regime": "micro", "eur_ron": 5.0})
    m = next(t for t in micro if t["name"] == "micro_revenue")
    assert m["limit"] == D("500000.00") and m["warn"] is True  # 100k EUR in 2026
    no_rate = thresholds(D("1"), year=2026, profile={"tax_regime": "micro"})
    assert "eur_ron" in no_rate[0]["note"]


def test_trend() -> None:
    t = trend([("2026-07", D("100")), ("2026-08", D("150")), ("2026-09", D("300"))])
    assert t["last"] == D("300") and t["average_before"] == D("125.00")
    assert t["change_pct"] == D("140.0")
    assert trend([])["last"] is None


def test_the_advice_template_is_valid_against_the_real_tools() -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.config.schema import DocumentsConfig
    from langclaw.documents import DocumentServices
    from langclaw.workflows.graph import parse_graph_spec

    path = Path(__file__).resolve().parent.parent / "ui" / "templates" / "monthly_advice.graph.json"
    names = {t.name for t in build_accounting_tools(DocumentServices(DocumentsConfig()))}
    assert "accounting_outlook" in names
    parse_graph_spec("monthly_advice", json.loads(path.read_text()), available_tools=names)


def _inv(key, direction, gross, due, paid="", partner="P", doc_type="invoice"):
    return {
        "bucket_key": key,
        "doc_type": doc_type,
        "amount": gross,
        "sender" if direction == "in" else "receiver": partner,
        "fields": {"direction": direction, "due_date": due, "paid_on": paid},
    }


def test_cash_position_ages_unpaid_invoices() -> None:
    from langclaw.accounting.outlook import cash_position

    on = date(2026, 9, 30)
    invoices = [
        _inv("s1", "out", 1000, "2026-10-10", partner="Alfa"),  # not due, within 30 days
        _inv("s2", "out", 500, "2026-09-20", partner="Alfa"),  # 10 days overdue
        _inv("s3", "out", 200, "2026-06-01", partner="Beta"),  # 121 days overdue
        _inv("s4", "out", 999, "2026-06-01", paid="2026-07-01"),  # paid: ignored
        _inv("p1", "in", 300, "2026-10-05", partner="Furnizor"),
        _inv("p2", "in", 50, "", partner="Fara scadenta"),  # no due date → not due
    ]
    cash = cash_position(invoices, on=on, bank_balance=D("2500.00"))
    rec, pay = cash["receivables"], cash["payables"]
    assert rec["total"] == D("1700.00") and rec["overdue"] == D("700.00")
    assert rec["buckets"] == {"not_due": D("1000.00"), "1-30": D("500.00"), "31-60": D("0.00"),
                              "61-90": D("0.00"), "90+": D("200.00")}  # fmt: skip
    assert rec["top_overdue"][0] == {"partner": "Alfa", "amount": D("500.00"), "invoices": 1}
    assert pay["total"] == D("350.00") and pay["overdue"] == D("0.00")
    assert cash["next_30_days"] == {"in": D("1000.00"), "out": D("300.00")}
    assert cash["bank_balance"] == D("2500.00")
    assert cash["projected_30_days"] == D("3200.00")


def test_the_monthly_loop_template_is_valid_against_the_real_tools() -> None:
    from unittest.mock import MagicMock

    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.config.schema import DocumentsConfig
    from langclaw.documents import DocumentServices, build_document_tools
    from langclaw.workflows.graph import parse_graph_spec

    services = DocumentServices(DocumentsConfig(efactura={"mode": "demo"}))
    names = {t.name for t in build_accounting_tools(services, bus=MagicMock())}
    names |= {t.name for t in build_document_tools(services, bus=MagicMock())}
    path = (
        Path(__file__).resolve().parent.parent / "ui" / "templates" / "accounting_month.graph.json"
    )
    spec = json.loads(path.read_text())
    parse_graph_spec("accounting_month", spec, available_tools=names)
    tools = [n["tool"] for n in spec["nodes"].values() if n["type"] == "tool"]
    assert tools == ["efactura_sync", "accounting_queue", "accounting_period_report",
                     "accounting_outlook"]  # fmt: skip


def test_aging_counts_what_is_left_after_partial_payments() -> None:
    from langclaw.accounting.outlook import cash_position

    inv = _inv("s1", "out", 1000, "2026-09-01", partner="Alfa")
    inv["fields"]["paid_amount"] = "400.00"
    cash = cash_position([inv], on=date(2026, 9, 30))
    assert cash["receivables"]["total"] == D("600.00")
