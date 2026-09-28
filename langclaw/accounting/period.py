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


_ACCOUNT_NO = re.compile(r"^\d{3,4}(\.\w+)?$")


def opening_entry(balances: dict[str, Any]) -> dict[str, Any]:
    """The entry that brings a client's balances into the journal when they
    start with langclaw: ``{account: balance}`` with debit − credit balances
    (negative = credit, e.g. 1012 capital, 401 suppliers). Zero balances are
    skipped.

    Raises:
        ValueError: a bad account or amount, nothing to post, or balances that
            don't sum to zero.
    """
    lines, total = [], Decimal(0)
    for account, value in sorted(balances.items()):
        account = str(account).strip()
        if not _ACCOUNT_NO.match(account):
            raise ValueError(f"{account!r} isn't an account number (e.g. 5121 or 401.01).")
        try:
            amount = Decimal(str(value)).quantize(_CENT)
        except InvalidOperation as exc:
            raise ValueError(f"Bad balance {value!r} for {account}.") from exc
        if not amount:
            continue
        total += amount
        debit, credit = (str(amount), "0") if amount > 0 else ("0", str(-amount))
        lines.append({"account": account, "debit": debit, "credit": credit,
                      "explanation": "Sold inițial"})  # fmt: skip
    if not lines:
        raise ValueError("No balances to post.")
    if total:
        raise ValueError(f"The balances don't balance: debit − credit is {total}, not 0.")
    return {"lines": lines, "reasoning": "Solduri inițiale preluate.",
            "legal_basis": "OMFP 1802/2014"}  # fmt: skip


#: (account prefix, the side its balance must not be on, why it's wrong there);
#: the first matching prefix wins.
_BALANCE_RULES: tuple[tuple[str, str, str], ...] = (
    ("5311", "credit", "More cash paid out than the register held (missing receipt?)."),
    ("512", "credit", "Bank account below zero: an overdraft, or a movement is missing."),
    ("581", "any", "Cash in transit should be zero once deposits/withdrawals are booked."),
    ("542", "credit", "An employee spent more than advanced: the company owes them."),
    ("28", "debit", "Accumulated depreciation can't have a debit balance."),
    ("29", "debit", "An impairment adjustment can't have a debit balance."),
    ("401", "debit", "The supplier was paid more than invoiced (advance or double payment)."),
    ("404", "debit", "The supplier was paid more than invoiced (advance or double payment)."),
    ("4111", "credit", "The customer paid more than invoiced (advance or double payment)."),
    ("3", "credit", "Stock can't have a credit balance: an exit without an entry."),
    ("2", "credit", "A fixed asset can't have a credit balance."),
)


def balance_anomalies(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accounts whose closing balance (``balance`` = debit − credit) is on the
    side it can't normally be on, each with the likely reason."""
    found = []
    for row in accounts:
        account, balance = str(row["account"]), _dec(row.get("balance"))
        if not balance:
            continue
        for prefix, wrong, why in _BALANCE_RULES:
            if not account.startswith(prefix):
                continue
            side = "debit" if balance > 0 else "credit"
            if wrong in ("any", side):
                found.append({"account": account, "balance": str(balance.quantize(_CENT)),
                              "problem": why})  # fmt: skip
            break
    return found


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


def paid_share(invoices: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    """The invoices paid (partly) in [*start*, *end*], each with its VAT breakdown
    scaled to the share paid then (``fields.payments`` by date) — what
    :func:`vat_summary` needs to show VAT on collection by rate."""
    out = []
    for row in invoices:
        f = row.get("fields") or {}
        gross = _dec(row.get("amount"))
        paid = sum((_dec(p.get("amount")) for p in f.get("payments") or []
                    if start.isoformat() <= str(p.get("date", ""))[:10] <= end.isoformat()),
                   Decimal(0))  # fmt: skip
        if not gross or not paid:
            continue
        ratio = paid / gross
        breakdown = [{**v, "taxable": str((_dec(v.get("taxable")) * ratio).quantize(_CENT)),
                      "vat": str((_dec(v.get("vat")) * ratio).quantize(_CENT))}
                     for v in f.get("vat_breakdown") or []]  # fmt: skip
        out.append({**row, "fields": {**f, "vat_breakdown": breakdown}})
    return out


def vat_due(lines: list[dict[str, Any]]) -> dict[str, Decimal]:
    """VAT that became due in a period, from its journal *lines*: credits to 4427
    (collected) and debits to 4426 (deductible). For VAT on collection, where the
    invoices' VAT waits on 4428 until they're paid; settlement lines (debits to
    4427, credits to 4426) don't count."""
    collected = sum((_dec(x.get("credit")) for x in lines
                     if str(x["account"]).split(".")[0] == "4427"), Decimal(0))  # fmt: skip
    deductible = sum((_dec(x.get("debit")) for x in lines
                      if str(x["account"]).split(".")[0] == "4426"), Decimal(0))  # fmt: skip
    net = (collected - deductible).quantize(_CENT)
    zero = Decimal(0).quantize(_CENT)
    return {"collected": collected.quantize(_CENT), "deductible": deductible.quantize(_CENT),
            "payable": max(net, zero), "refundable": max(-net, zero)}  # fmt: skip


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
    (``vat_period: quarterly``). With VAT on collection the payments move the paid
    share of 4428 into 4426/4427 as they're booked, so what's settled is only
    the VAT that became due; what's unpaid stays on 4428."""
    if not profile.get("vat_payer"):
        return False
    _, end = parse_period(period)
    return profile.get("vat_period") != "quarterly" or end.month % 3 == 0


def vat_settlement(
    *, deductible: Decimal, collected: Decimal, carried: Decimal = Decimal(0)
) -> dict[str, Any] | None:
    """The settlement entry for the balances of 4426 (*deductible*, debit) and 4427
    (*collected*, credit): 4427 = 4426 + 4423 (payable) or 4427 + 4424 (refundable)
    = 4426. *carried* is VAT still to recover from earlier periods (the 4424
    debit balance, not refunded): it's set off against what's payable now
    (credit 4424), and whatever it doesn't cover stays on 4424. ``None`` when
    there's nothing to settle."""
    d, c = deductible.quantize(_CENT), collected.quantize(_CENT)
    carried = max(carried.quantize(_CENT), Decimal(0))
    if not d and not c:
        return None

    def line(account: str, debit: Decimal, credit: Decimal) -> dict[str, str]:
        return {
            "account": account,
            "debit": str(debit) if debit else "0",
            "credit": str(credit) if credit else "0",
            "explanation": "Regularizare TVA",
        }

    lines, used = [], Decimal(0)
    if c:
        lines.append(line("4427", c, Decimal(0)))
    if c > d:
        if d:
            lines.append(line("4426", Decimal(0), d))
        used = min(carried, c - d)
        if used:
            lines.append(line("4424", Decimal(0), used))
        if c - d - used:
            lines.append(line("4423", Decimal(0), c - d - used))
    else:
        if d > c:
            lines.append(line("4424", d - c, Decimal(0)))
        if d:
            lines.append(line("4426", Decimal(0), d))
    reasoning = f"Regularizare TVA: colectată {c}, deductibilă {d}."
    if used:
        reasoning += f" Compensat cu TVA de recuperat din perioadele anterioare: {used}."
    return {
        "lines": lines,
        "reasoning": reasoning,
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
