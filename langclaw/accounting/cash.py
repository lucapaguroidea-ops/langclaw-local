"""
Cash register — the daily Z report (raport Z) as a journal entry.

A Z report gives the day's gross sales per VAT rate. The entry is
D 5311 (cash) for the total / C the revenue account for the net (``707`` sale
of goods by default, or the client profile's ``cash_revenue_account``) /
C 4427 for the VAT, worked out per rate from the gross amounts. Rates must be
valid on the day (:mod:`langclaw.accounting.vat`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from langclaw.accounting.vat import allowed_vat_rates

_CENT = Decimal("0.01")
CASH_ACCOUNT = "5311"
DEFAULT_REVENUE_ACCOUNT = "707"


def z_report_entry(
    day: date, lines: list[dict[str, Any]], *, revenue_account: str = DEFAULT_REVENUE_ACCOUNT
) -> dict[str, Any]:
    """The entry for a Z report whose *lines* are ``{"rate", "gross"}``.

    Raises:
        ValueError: no lines, a bad amount, or a rate not valid on *day*.
    """
    if not lines:
        raise ValueError("The Z report is empty: give its VAT-rate lines (rate, gross).")
    valid = allowed_vat_rates(day)
    gross_total = vat_total = Decimal(0)
    for line in lines:
        try:
            rate = Decimal(str(line["rate"]))
            gross = Decimal(str(line["gross"])).quantize(_CENT)
        except (KeyError, InvalidOperation) as exc:
            raise ValueError(f"Bad Z report line {line!r}: needs rate and gross.") from exc
        if rate not in valid:
            rates = ", ".join(f"{r.normalize()}%" for r in sorted(valid, reverse=True))
            raise ValueError(f"{rate.normalize()}% isn't a VAT rate on {day}; valid: {rates}.")
        gross_total += gross
        vat_total += (gross * rate / (100 + rate)).quantize(_CENT)
    net = gross_total - vat_total
    note = f"Raport Z {day.isoformat()}"
    entry_lines = [
        {"account": CASH_ACCOUNT, "debit": str(gross_total), "credit": "0", "explanation": note},
        {"account": revenue_account, "debit": "0", "credit": str(net), "explanation": note},
    ]
    if vat_total:
        entry_lines.append(
            {"account": "4427", "debit": "0", "credit": str(vat_total), "explanation": note}
        )
    return {
        "lines": entry_lines,
        "totals": {"gross": gross_total, "net": net, "vat": vat_total},
        "reasoning": f"{note}: vânzări cu numerar.",
        "legal_basis": "OMFP 1802/2014; Codul fiscal art. 291",
    }
