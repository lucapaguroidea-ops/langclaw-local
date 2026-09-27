"""
Period close — what a month looks like before it's locked.

Pure functions over what's already stored: :func:`trial_balance` from posted
journal lines, :func:`vat_summary` from the month's invoices (the figures that
go into the D300 VAT return — a draft for the accountant, not the ANAF form),
and :func:`blockers` (invoices still without an entry). Locking lives in
:class:`langclaw.accounting.journal.Journal` (``close_period``).
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

_CENT = Decimal("0.01")
_PERIOD = re.compile(r"^(\d{4})-(\d{2})$")
INVOICE_TYPES = ("invoice", "credit_note")
#: Statuses that mean an invoice has its entry.
BOOKED = ("posted", "exported")


def parse_period(period: str) -> tuple[date, date]:
    """``"2026-09"`` → first and last day of the month.

    Raises:
        ValueError: not a ``YYYY-MM`` month.
    """
    match = _PERIOD.match((period or "").strip())
    if not match or not 1 <= int(match[2]) <= 12:
        raise ValueError(f"Period {period!r} must be a month as YYYY-MM (e.g. 2026-09).")
    year, month = int(match[1]), int(match[2])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0)).quantize(_CENT)
    except InvalidOperation:
        return Decimal(0).quantize(_CENT)


def trial_balance(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Debit / credit turnover and balance (debit − credit) per account."""
    totals: dict[str, list[Decimal]] = {}
    for line in lines:
        pair = totals.setdefault(str(line["account"]), [Decimal(0), Decimal(0)])
        pair[0] += _dec(line.get("debit"))
        pair[1] += _dec(line.get("credit"))
    accounts = [
        {"account": a, "debit": d.quantize(_CENT), "credit": c.quantize(_CENT),
         "balance": (d - c).quantize(_CENT)}
        for a, (d, c) in sorted(totals.items())
    ]  # fmt: skip
    debit = sum((r["debit"] for r in accounts), Decimal(0)).quantize(_CENT)
    credit = sum((r["credit"] for r in accounts), Decimal(0)).quantize(_CENT)
    return {"accounts": accounts, "debit": debit, "credit": credit, "balanced": debit == credit}


def _rate(value: Decimal) -> str:
    return format(value.normalize(), "f")


def vat_summary(invoices: list[dict[str, Any]]) -> dict[str, Any]:
    """VAT by rate for sales and purchases, and the resulting payable / refundable.

    Credit notes count negative. Reverse-charge purchases (category ``AE``) add
    the same VAT to both collected and deductible.
    """
    sales: dict[Decimal, list[Decimal]] = {}
    purchases: dict[Decimal, list[Decimal]] = {}
    reverse = [Decimal(0), Decimal(0)]
    for row in invoices:
        f = row.get("fields") or {}
        sign = -1 if row.get("doc_type") == "credit_note" else 1
        for v in f.get("vat_breakdown") or []:
            rate, taxable, vat = _dec(v.get("rate")), _dec(v.get("taxable")), _dec(v.get("vat"))
            taxable, vat = sign * abs(taxable), sign * abs(vat)
            if f.get("direction") == "in" and str(v.get("category", "")).upper() == "AE":
                vat = (taxable * rate / 100).quantize(_CENT)
                reverse[0] += taxable
                reverse[1] += vat
                continue
            bucket = (sales if f.get("direction") == "out" else purchases).setdefault(
                rate, [Decimal(0), Decimal(0)]
            )
            bucket[0] += taxable
            bucket[1] += vat

    def rows(groups: dict[Decimal, list[Decimal]]) -> list[dict[str, Any]]:
        return [
            {"rate": _rate(r), "taxable": t.quantize(_CENT), "vat": v.quantize(_CENT)}
            for r, (t, v) in sorted(groups.items(), reverse=True)
        ]

    collected = sum((v for _, v in sales.values()), Decimal(0)) + reverse[1]
    deductible = sum((v for _, v in purchases.values()), Decimal(0)) + reverse[1]
    net = (collected - deductible).quantize(_CENT)
    zero = Decimal(0).quantize(_CENT)
    return {
        "sales": rows(sales),
        "purchases": rows(purchases),
        "reverse_charge": {"taxable": reverse[0].quantize(_CENT), "vat": reverse[1]},
        "collected": collected.quantize(_CENT),
        "deductible": deductible.quantize(_CENT),
        "payable": max(net, zero),
        "refundable": max(-net, zero),
    }


def blockers(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Invoices in the period that don't have a journal entry yet."""
    return [
        {"bucket_key": d["bucket_key"], "status": d.get("status", "")}
        for d in documents
        if d.get("doc_type") in INVOICE_TYPES and d.get("status") not in BOOKED
    ]


def document_state(documents: list[dict[str, Any]], expected: list[Any] | None) -> dict[str, Any]:
    """Which of the client's expected monthly documents are in, and what's unreviewed.

    Args:
        documents: The month's ``documents`` rows (any type).
        expected: The client profile's ``expected_documents`` — doc types, or
            ``{"doc_type", "label"?}`` objects (e.g. ``bank_statement``, ``payroll``).

    Raises:
        ValueError: an ``expected_documents`` entry has no doc type.
    """
    wanted = []
    for item in expected or []:
        spec = {"doc_type": item} if isinstance(item, str) else dict(item)
        if not spec.get("doc_type"):
            raise ValueError(f"expected_documents entry {item!r} needs a doc_type.")
        wanted.append(
            {"doc_type": spec["doc_type"], "label": spec.get("label") or spec["doc_type"]}
        )
    state = []
    for spec in wanted:
        keys = [d["bucket_key"] for d in documents if d.get("doc_type") == spec["doc_type"]]
        state.append({**spec, "count": len(keys), "keys": keys})
    return {
        "expected": state,
        "missing": [
            {"doc_type": s["doc_type"], "label": s["label"]} for s in state if not s["count"]
        ],
        "needs_review": [d["bucket_key"] for d in documents if d.get("status") == "needs_review"],
    }
