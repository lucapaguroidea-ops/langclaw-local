"""
Results — profit and loss from the journal, and an income-tax estimate.

Revenue is class 7 (credit − debit, so 709 discounts reduce it), expenses are
class 6 except the income-tax accounts (691 profit tax, 697/698 micro and other
taxes on income). The tax estimate is a planning figure: micro-enterprise tax on
revenue at the profile's ``micro_rate`` (1% by default), or 16% profit tax on the
accounting result — before non-deductible expenses, loss carry-forward and
sponsorship credits, which stay with the accountant.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

_CENT = Decimal("0.01")
INCOME_TAX_ACCOUNTS = ("691", "697", "698")
PROFIT_TAX_RATE = Decimal(16)
DEFAULT_MICRO_RATE = Decimal(1)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except InvalidOperation:
        return Decimal(0)


def profit_and_loss(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Revenue, expenses and result from journal *lines* (``account``/``debit``/``credit``)."""
    revenue: dict[str, Decimal] = {}
    expenses: dict[str, Decimal] = {}
    tax = Decimal(0)
    for line in lines:
        account = str(line["account"])
        base = account.split(".", 1)[0]
        net = _dec(line.get("debit")) - _dec(line.get("credit"))
        if base.startswith(INCOME_TAX_ACCOUNTS):
            tax += net
        elif base.startswith("7"):
            revenue[account] = revenue.get(account, Decimal(0)) - net
        elif base.startswith("6"):
            expenses[account] = expenses.get(account, Decimal(0)) + net

    def rows(groups: dict[str, Decimal]) -> list[dict[str, Any]]:
        return [{"account": a, "amount": v.quantize(_CENT)} for a, v in sorted(groups.items())]

    total_revenue = sum(revenue.values(), Decimal(0)).quantize(_CENT)
    total_expenses = sum(expenses.values(), Decimal(0)).quantize(_CENT)
    return {
        "revenue": total_revenue,
        "expenses": total_expenses,
        "result": total_revenue - total_expenses,
        "income_tax_booked": tax.quantize(_CENT),
        "revenue_accounts": rows(revenue),
        "expense_accounts": rows(expenses),
    }


def tax_estimate(pl: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """The income tax on *pl* under the client's regime (``tax_regime``: micro | profit)."""
    if profile.get("tax_regime") == "micro":
        rate = _dec(profile.get("micro_rate") or DEFAULT_MICRO_RATE)
        base = pl["revenue"]
        note = "Micro-enterprise tax on revenue; check revenue exclusions (Codul fiscal art. 53)."
        regime = "micro"
    else:
        rate, base, regime = PROFIT_TAX_RATE, max(pl["result"], Decimal(0)), "profit"
        note = ("16% of the accounting result, before non-deductible expenses, "
                "loss carry-forward and sponsorship credits.")  # fmt: skip
    return {
        "regime": regime,
        "base": base,
        "rate": rate,
        "tax": (base * rate / 100).quantize(_CENT),
        "note": note,
    }
