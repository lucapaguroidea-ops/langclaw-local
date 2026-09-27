"""Results (P&L) from the journal and the income-tax estimate."""

from __future__ import annotations

from decimal import Decimal

from langclaw.accounting.results import profit_and_loss, tax_estimate

D = Decimal
LINES = [
    {"account": "4111", "debit": 1210, "credit": 0},
    {"account": "704", "debit": 0, "credit": 1000},
    {"account": "4427", "debit": 0, "credit": 210},
    {"account": "628", "debit": 300, "credit": 0},
    {"account": "6811", "debit": 100, "credit": 0},
    {"account": "691", "debit": 50, "credit": 0},  # income tax: not an operating expense
    {"account": "7588", "debit": 0, "credit": 20},
    {"account": "709", "debit": 20, "credit": 0},  # discounts granted reduce revenue
]


def test_profit_and_loss_by_class() -> None:
    pl = profit_and_loss(LINES)
    assert pl["revenue"] == D("1000.00")  # 1000 + 20 − 20
    assert pl["expenses"] == D("400.00")
    assert pl["result"] == D("600.00")
    assert pl["income_tax_booked"] == D("50.00")
    assert {r["account"] for r in pl["revenue_accounts"]} == {"704", "7588", "709"}


def test_tax_estimate_by_regime() -> None:
    pl = profit_and_loss(LINES)
    micro = tax_estimate(pl, {"tax_regime": "micro"})
    assert micro == {"regime": "micro", "base": D("1000.00"), "rate": D("1"),
                     "tax": D("10.00"), "note": micro["note"]}  # fmt: skip
    assert tax_estimate(pl, {"tax_regime": "micro", "micro_rate": 3})["tax"] == D("30.00")
    profit = tax_estimate(pl, {})
    assert profit["regime"] == "profit" and profit["tax"] == D("96.00")
    assert "non-deductible" in profit["note"]
    loss = tax_estimate(profit_and_loss([{"account": "628", "debit": 5, "credit": 0}]), {})
    assert loss["tax"] == D("0.00")
