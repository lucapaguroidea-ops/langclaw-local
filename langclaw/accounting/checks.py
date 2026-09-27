"""
Deterministic checks on a proposed journal entry — the model proposes, this decides.

A proposal is ``{"lines": [{"account", "debit", "credit", "explanation"?}], ...}``
for one invoice (its ``documents`` row fields). :func:`check_proposal` returns
every problem found (empty = OK to post). Rules follow the Romanian chart of
accounts (OMFP 1802/2014) at the level of account classes and the usual
counterparts; they don't replace an accountant's judgement on the choice of a
specific expense account.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from langclaw.accounting.vat import allowed_vat_rates

_CENT = Decimal("0.01")
_ACCOUNT = re.compile(r"^\d{3,4}(\.\d{1,6})?$")
#: Counterpart accounts for a supplier invoice / a customer invoice.
SUPPLIER_ACCOUNTS = ("401", "404", "408", "462")
CUSTOMER_ACCOUNTS = ("411", "4111", "418", "461")
DEDUCTIBLE_VAT, COLLECTED_VAT, PENDING_VAT = "4426", "4427", "4428"


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0)).quantize(_CENT)
    except InvalidOperation:
        return Decimal("NaN")


def _base(account: str) -> str:
    return account.split(".", 1)[0]


def _sum(lines: list[dict], side: str, *prefixes: str) -> Decimal:
    return sum(
        (_dec(line.get(side)) for line in lines if _base(line["account"]).startswith(prefixes)),
        Decimal(0),
    )


def check_proposal(
    proposal: dict[str, Any], invoice: dict[str, Any], *, profile: dict[str, Any]
) -> list[str]:
    """Problems with *proposal* for *invoice* given the client's *profile*.

    Args:
        proposal: ``{"lines": [{"account", "debit", "credit"}]}``.
        invoice: The document's facts — ``direction`` (in/out), ``document_date``,
            ``total_net``, ``total_vat``, ``amount`` (gross), ``vat_breakdown``.
        profile: The client's company profile (``vat_payer``, ``vat_on_collection``).
    """
    problems: list[str] = []
    lines = [dict(line) for line in proposal.get("lines") or []]
    if not lines:
        return ["The proposal has no lines."]
    well_formed = []
    for line in lines:
        account = str(line.get("account", "")).strip()
        line["account"] = account
        debit, credit = _dec(line.get("debit")), _dec(line.get("credit"))
        if not _ACCOUNT.match(account):
            problems.append(f"Account {account!r} isn't a chart account (e.g. 605, 4426, 401.1).")
        elif debit.is_nan() or credit.is_nan() or debit < 0 or credit < 0:
            problems.append(f"Account {account}: amounts must be non-negative numbers.")
        elif (debit > 0) == (credit > 0):
            problems.append(f"Account {account}: a line is either a debit or a credit.")
        else:
            well_formed.append(line)
    if len(well_formed) < len(lines):
        # Report what can still be judged, but never pass a malformed entry.
        lines = well_formed
        if not lines:
            return problems

    total_debit, total_credit = _sum(lines, "debit", ""), _sum(lines, "credit", "")
    if total_debit != total_credit:
        problems.append(f"The entry doesn't balance: debit {total_debit} ≠ credit {total_credit}.")

    gross = _dec(invoice.get("amount"))
    vat = _dec(invoice.get("total_vat"))
    incoming = invoice.get("direction", "in") != "out"
    vat_payer = bool(profile.get("vat_payer", True))
    vat_account = (
        PENDING_VAT
        if profile.get("vat_on_collection")
        else (DEDUCTIBLE_VAT if incoming else COLLECTED_VAT)
    )

    if incoming:
        owed = _sum(lines, "credit", *SUPPLIER_ACCOUNTS)
        if owed != gross:
            problems.append(
                f"The supplier account (401/404/408/462) should be credited {gross}, not {owed}."
            )
        for line in lines:
            base = _base(line["account"])
            if _dec(line["debit"]) > 0 and not base.startswith(("2", "3", "6", "44")):
                problems.append(
                    f"Account {line['account']} can't be debited for a purchase "
                    "(expected class 2, 3 or 6, or a VAT account)."
                )
        booked_vat = _sum(lines, "debit", "442")
    else:
        owed = _sum(lines, "debit", *CUSTOMER_ACCOUNTS)
        if owed != gross:
            problems.append(
                f"The customer account (4111/418/461) should be debited {gross}, not {owed}."
            )
        for line in lines:
            base = _base(line["account"])
            if _dec(line["credit"]) > 0 and not base.startswith(("7", "44")):
                problems.append(
                    f"Account {line['account']} can't be credited for a sale "
                    "(expected class 7, or a VAT account)."
                )
        booked_vat = _sum(lines, "credit", "442")

    used_vat = {
        _base(line["account"]) for line in lines if _base(line["account"]).startswith("442")
    }
    if not vat_payer:
        if used_vat:
            problems.append(
                "The client is not a VAT payer: no VAT account — the VAT is part of the cost."
            )
    else:
        if booked_vat != vat:
            problems.append(f"VAT booked {booked_vat} ≠ the invoice's VAT {vat}.")
        wrong = used_vat - {vat_account}
        if wrong:
            why = "VAT on collection" if profile.get("vat_on_collection") else "this invoice"
            problems.append(f"Use {vat_account} for {why}, not {', '.join(sorted(wrong))}.")

    issued = str(invoice.get("document_date") or "")[:10]
    if issued:
        try:
            valid = allowed_vat_rates(date.fromisoformat(issued))
        except ValueError:
            valid = set()
        for vat_line in invoice.get("vat_breakdown") or []:
            rate = _dec(vat_line.get("rate")).normalize()
            if valid and rate not in {r.normalize() for r in valid}:
                shown = ", ".join(f"{r.normalize()}%" for r in sorted(valid, reverse=True))
                problems.append(
                    f"VAT rate {rate}% wasn't valid on {issued} (valid: {shown}) — check the invoice."
                )
    return problems
