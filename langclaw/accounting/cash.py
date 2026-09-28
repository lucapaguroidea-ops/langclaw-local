"""
Cash register — the daily Z report (raport Z) as a journal entry.

A Z report gives the day's gross sales per VAT rate. The entry is
D 5311 (cash) for the total / C the revenue account for the net (``707`` sale
of goods by default, or the client profile's ``cash_revenue_account``) /
C 4427 for the VAT, worked out per rate from the gross amounts. Rates must be
valid on the day (:mod:`langclaw.accounting.vat`).

:func:`cash_book` is the cash register book (registru de casă) read back from
5311: opening balance, each day's receipts and payments, and the closing
balance, with the days the cash went negative (money paid out that the register
never had — usually a missing receipt) or above the client's ``cash_limit``.

:func:`cash_expense_entry` books a purchase paid in cash with a receipt and no
invoice (bon fiscal): D expense or stock / D 4426 / C 5311 — or C 542 when an
employee paid it from a cash advance (:func:`advance_entry`: D 542 / C 5311 when
the advance is given, D 5311 / C 542 for what they bring back).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from langclaw.accounting.vat import allowed_vat_rates

_CENT = Decimal("0.01")
CASH_ACCOUNT = "5311"
ADVANCE_ACCOUNT = "542"
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


def cash_book(
    opening: Decimal, lines: list[dict[str, Any]], *, limit: Decimal | None = None
) -> dict[str, Any]:
    """Day-by-day cash book from *opening* and 5311 journal *lines*
    (``entry_date``, ``bucket_key``, ``debit``, ``credit``, ``explanation``)."""
    balance = Decimal(opening).quantize(_CENT)
    days: dict[date, dict[str, Any]] = {}
    for line in sorted(lines, key=lambda x: x["entry_date"]):
        empty = {"receipts": Decimal(0), "payments": Decimal(0), "entries": []}
        day = days.setdefault(line["entry_date"], empty)
        debit, credit = Decimal(line["debit"]), Decimal(line["credit"])
        day["receipts"] += debit
        day["payments"] += credit
        day["entries"].append({"bucket_key": line["bucket_key"], "in": str(debit.quantize(_CENT)),
                               "out": str(credit.quantize(_CENT)),
                               "explanation": line.get("explanation") or ""})  # fmt: skip
    out, problems = [], []
    for on, day in sorted(days.items()):
        start = balance
        balance = (balance + day["receipts"] - day["payments"]).quantize(_CENT)
        out.append({"day": on.isoformat(), "opening": str(start),
                    "receipts": str(day["receipts"].quantize(_CENT)),
                    "payments": str(day["payments"].quantize(_CENT)),
                    "closing": str(balance), "entries": day["entries"]})  # fmt: skip
        if balance < 0:
            problems.append({"day": on.isoformat(), "problem": f"Cash negative: {balance}; "
                             "a receipt is probably missing or booked late."})  # fmt: skip
        if limit is not None and balance > limit:
            problems.append({"day": on.isoformat(), "problem": f"Cash above the "
                             f"{Decimal(limit).quantize(_CENT)} limit: {balance}."})  # fmt: skip
    return {"opening": str(Decimal(opening).quantize(_CENT)), "closing": str(balance),
            "days": out, "problems": problems}  # fmt: skip


def cash_expense_entry(
    day: date,
    gross: str,
    rate: Any,
    account: str,
    *,
    deduct_vat: bool,
    paid_from: str = CASH_ACCOUNT,
) -> dict[str, Any]:
    """The entry for a *gross* cash purchase at VAT *rate* booked on *account*
    (class 6 expense, class 3 stock or class 2 asset). With *deduct_vat* the VAT
    goes to 4426, otherwise it stays in the cost. *paid_from* is 5311, or 542 for
    a receipt settling an employee's advance.

    Raises:
        ValueError: a bad amount or account, or a rate not valid on *day*.
    """
    try:
        amount = Decimal(str(gross)).quantize(_CENT)
        vat_rate = Decimal(str(rate))
    except InvalidOperation as exc:
        raise ValueError(f"Bad amount {gross!r} or rate {rate!r}.") from exc
    if amount <= 0:
        raise ValueError(f"The amount must be above 0, not {amount}.")
    account = str(account).strip()
    if account[:1] not in ("2", "3", "6") or account[:2] in ("28", "29", "39"):
        raise ValueError(f"{account} isn't an expense (6xx), stock (3xx) or asset (2xx) "
                         "account for a cash purchase.")  # fmt: skip
    valid = allowed_vat_rates(day)
    if vat_rate not in valid:
        rates = ", ".join(f"{r.normalize()}%" for r in sorted(valid, reverse=True))
        raise ValueError(f"{vat_rate.normalize()}% isn't a VAT rate on {day}; valid: {rates}.")
    vat = (amount * vat_rate / (100 + vat_rate)).quantize(_CENT) if deduct_vat else Decimal(0)
    note = f"Bon fiscal {day.isoformat()}"
    lines = [{"account": account, "debit": str(amount - vat), "credit": "0", "explanation": note}]
    if vat:
        lines.append({"account": "4426", "debit": str(vat), "credit": "0", "explanation": note})
    lines.append({"account": paid_from, "debit": "0", "credit": str(amount),
                  "explanation": note})  # fmt: skip
    return {
        "lines": lines,
        "totals": {"gross": amount, "net": amount - vat, "vat": vat},
        "reasoning": f"{note}: cheltuială plătită cu numerar.",
        "legal_basis": "OMFP 1802/2014; Codul fiscal art. 299, 319",
    }


def advance_entry(amount: str, employee: str, *, returned: bool = False) -> dict[str, Any]:
    """Cash advanced to *employee* (D 542 / C 5311), or, *returned*, the unspent
    part they bring back (D 5311 / C 542).

    Raises:
        ValueError: no employee or an amount not above 0.
    """
    if not (employee or "").strip():
        raise ValueError("Name the employee who gets or returns the advance.")
    try:
        value = Decimal(str(amount)).quantize(_CENT)
    except InvalidOperation as exc:
        raise ValueError(f"Bad amount {amount!r}.") from exc
    if value <= 0:
        raise ValueError(f"The amount must be above 0, not {value}.")
    note = f"{'Restituire avans' if returned else 'Avans de trezorerie'} {employee.strip()}"
    debit, credit = (CASH_ACCOUNT, ADVANCE_ACCOUNT) if returned else (ADVANCE_ACCOUNT, CASH_ACCOUNT)
    return {
        "lines": [
            {"account": debit, "debit": str(value), "credit": "0", "explanation": note},
            {"account": credit, "debit": "0", "credit": str(value), "explanation": note},
        ],
        "reasoning": f"{note}.",
        "legal_basis": "OMFP 1802/2014",
    }
