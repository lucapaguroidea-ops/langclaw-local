"""Forward-looking facts (RO): deadlines, thresholds, VAT trend, the advice template."""

from __future__ import annotations

import json
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
