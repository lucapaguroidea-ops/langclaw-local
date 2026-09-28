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


def resolve_period(period: str, *, today: date | None = None) -> str:
    """*period* as ``YYYY-MM``; ``""`` or ``"last"`` means the month before *today*
    (so a scheduled run can always ask for "last month").

    Raises:
        ValueError: not a ``YYYY-MM`` month.
    """
    value = (period or "").strip()
    if value in ("", "last"):
        today = today or date.today()
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return f"{year:04d}-{month:02d}"
    parse_period(value)
    return value


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


_SHEET_PAIRS = ("opening", "previous", "month", "total", "closing")


def trial_balance_sheet(
    before_year: list[dict[str, Any]],
    earlier: list[dict[str, Any]],
    month: list[dict[str, Any]],
) -> dict[str, Any]:
    """The trial balance (balanța de verificare) with its five column pairs:
    opening balance at the start of the year (from *before_year* lines), earlier
    turnover this year (*earlier*), the *month*'s turnover, total sums, and the
    closing balance. Each balance goes on its debit or credit side.
    """
    rows: dict[str, dict[str, Decimal]] = {}

    def row(account: str) -> dict[str, Decimal]:
        return rows.setdefault(account, {f"{p}_{s}": Decimal(0) for p in _SHEET_PAIRS
                                         for s in ("debit", "credit")})  # fmt: skip

    for line in before_year:
        r = row(str(line["account"]))
        r["opening_debit"] += _dec(line.get("debit")) - _dec(line.get("credit"))
    for pair, lines in (("previous", earlier), ("month", month)):
        for line in lines:
            r = row(str(line["account"]))
            r[f"{pair}_debit"] += _dec(line.get("debit"))
            r[f"{pair}_credit"] += _dec(line.get("credit"))
    accounts = []
    for account, r in sorted(rows.items()):
        net = r["opening_debit"]
        r["opening_debit"], r["opening_credit"] = max(net, Decimal(0)), max(-net, Decimal(0))
        for side in ("debit", "credit"):
            r[f"total_{side}"] = r[f"opening_{side}"] + r[f"previous_{side}"] + r[f"month_{side}"]
        net = r["total_debit"] - r["total_credit"]
        r["closing_debit"], r["closing_credit"] = max(net, Decimal(0)), max(-net, Decimal(0))
        accounts.append({"account": account, **{k: v.quantize(_CENT) for k, v in r.items()}})
    totals = {k: sum((a[k] for a in accounts), Decimal(0)).quantize(_CENT)
              for k in accounts[0] if k != "account"} if accounts else {}  # fmt: skip
    balanced = all(totals.get(f"{p}_debit") == totals.get(f"{p}_credit") for p in _SHEET_PAIRS)
    return {"accounts": accounts, "totals": totals, "balanced": balanced}


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


def settles_vat(period: str, profile: dict[str, Any]) -> bool:
    """Whether the month's close settles VAT: VAT payers, monthly or at quarter end
    (``vat_period: quarterly``). Not for VAT on collection — the 4428 → 4427
    transfer depends on payments and stays with the accountant."""
    if not profile.get("vat_payer") or profile.get("vat_on_collection"):
        return False
    _, end = parse_period(period)
    return profile.get("vat_period") != "quarterly" or end.month % 3 == 0


def vat_settlement(*, deductible: Decimal, collected: Decimal) -> dict[str, Any] | None:
    """The settlement entry for the balances of 4426 (*deductible*, debit) and 4427
    (*collected*, credit): 4427 = 4426 + 4423 (payable) or 4427 + 4424 (refundable)
    = 4426. ``None`` when both are zero."""
    d, c = deductible.quantize(_CENT), collected.quantize(_CENT)
    if not d and not c:
        return None

    def line(account: str, debit: Decimal, credit: Decimal) -> dict[str, str]:
        return {
            "account": account,
            "debit": str(debit) if debit else "0",
            "credit": str(credit) if credit else "0",
            "explanation": "Regularizare TVA",
        }

    lines = []
    if c:
        lines.append(line("4427", c, Decimal(0)))
    if c > d:
        if d:
            lines.append(line("4426", Decimal(0), d))
        lines.append(line("4423", Decimal(0), c - d))
    else:
        if d > c:
            lines.append(line("4424", d - c, Decimal(0)))
        if d:
            lines.append(line("4426", Decimal(0), d))
    return {
        "lines": lines,
        "reasoning": f"Regularizare TVA: colectată {c}, deductibilă {d}.",
        "legal_basis": "OMFP 1802/2014; Codul fiscal art. 316",
    }


def d394_rows(invoices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The D394 figures: per partner (with a tax ID), direction (``out`` supplies /
    ``in`` purchases), VAT rate and type (``normal`` / ``reverse_charge``) — invoice
    count, taxable base and VAT. Credit notes count negative. A draft for the
    accountant, not the ANAF declaration file."""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in invoices:
        f = row.get("fields") or {}
        direction = f.get("direction", "in")
        cui = str(f.get("customer_cui" if direction == "out" else "supplier_cui") or "")
        if not cui:
            continue
        name = row.get("receiver" if direction == "out" else "sender") or ""
        sign = -1 if row.get("doc_type") == "credit_note" else 1
        for v in f.get("vat_breakdown") or []:
            rate, taxable, vat = _dec(v.get("rate")), _dec(v.get("taxable")), _dec(v.get("vat"))
            kind = "normal"
            if direction == "in" and str(v.get("category", "")).upper() == "AE":
                kind, vat = "reverse_charge", (taxable * rate / 100).quantize(_CENT)
            key = (direction, cui, _rate(rate), kind)
            g = groups.setdefault(key, {"direction": direction, "cui": cui, "partner": name,
                                        "rate": _rate(rate), "type": kind, "invoices": 0,
                                        "taxable": Decimal(0), "vat": Decimal(0)})  # fmt: skip
            g["invoices"] += 1
            g["taxable"] += sign * abs(taxable)
            g["vat"] += sign * abs(vat)
    rows = sorted(groups.values(), key=lambda g: (g["direction"], g["cui"], g["rate"]))
    for g in rows:
        g["taxable"], g["vat"] = g["taxable"].quantize(_CENT), g["vat"].quantize(_CENT)
    return rows
