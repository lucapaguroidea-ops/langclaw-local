"""Invoices that don't come from e-Factura (a scan, a PDF, a photo): the facts a
model read from the page → the same ``fields`` an e-Factura invoice carries
(``direction``, the parties' tax ids, net / VAT / breakdown by rate), checked
the same way, so the accounting tools treat both alike.

Nothing here trusts the model's arithmetic: every total is re-added, each rate
must be in force on the invoice date, and whatever doesn't hold is listed in
``problems`` (the document is then filed as ``needs_review``).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from langclaw.accounting.vat import allowed_vat_rates

_CENT = Decimal("0.01")
_TOLERANCE = Decimal("0.05")  # per-row rounding a printed invoice may carry


def _amount(value: Any) -> Decimal | None:
    """``"1.234,50"`` / ``"1,234.50"`` / ``1234.5`` / ``"21%"`` → ``Decimal``."""
    if value in (None, ""):
        return None
    text = re.sub(r"[^\d,.\-]", "", str(value))
    if "," in text and "." in text:  # the last separator is the decimal one
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else (
            text.replace(".", "").replace(",", "."))  # fmt: skip
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) != 3 else text.replace(",", "")
    try:
        return Decimal(text).quantize(_CENT)
    except InvalidOperation:
        return None


def _cui(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return f"RO{digits}" if digits else ""


def _rate(value: Decimal) -> str:
    return str(value.normalize()) if value == value.to_integral() else str(value)


def invoice_fields(facts: dict[str, Any], *, own_cif: str) -> dict[str, Any]:
    """Normalize *facts* read from an invoice for the client *own_cif*.

    Args:
        facts: ``invoice_number``, ``kind`` (``invoice`` / ``credit_note``),
            ``supplier_name`` / ``supplier_cui``, ``customer_name`` /
            ``customer_cui``, ``issue_date`` / ``due_date`` (YYYY-MM-DD),
            ``currency``, ``total_net`` / ``total_vat`` / ``total_gross`` and
            ``vat_breakdown`` rows (``rate``, ``taxable``, ``vat``, optional
            ``category``: ``S`` standard, ``Z`` zero, ``AE`` reverse charge).
        own_cif: The client's tax id, to tell a sale from a purchase.

    Returns:
        ``{"doc_type", "amount", "currency", "document_date", "fields",
        "problems"}``; ``fields`` matches an e-Factura invoice's.
    """
    problems: list[str] = []
    number = str(facts.get("invoice_number") or "").strip()
    if not number:
        problems.append("No invoice number was read.")
    issued: date | None = None
    try:
        issued = date.fromisoformat(str(facts.get("issue_date") or "")[:10])
    except ValueError:
        problems.append("No issue date (YYYY-MM-DD) was read.")
    supplier, customer, own = (_cui(facts.get("supplier_cui")), _cui(facts.get("customer_cui")),
                               _cui(own_cif))  # fmt: skip
    if own and supplier == own:
        direction = "out"
    elif own and customer == own:
        direction = "in"
    else:
        direction = ""
        problems.append(f"The client ({own or 'no tax id'}) is neither the supplier "
                        f"({supplier or '?'}) nor the customer ({customer or '?'}).")  # fmt: skip

    net, vat, gross = (_amount(facts.get(k)) for k in ("total_net", "total_vat", "total_gross"))
    rows = []
    for row in facts.get("vat_breakdown") or []:
        rate, taxable, amount = (_amount(row.get(k)) for k in ("rate", "taxable", "vat"))
        if rate is None or taxable is None:
            problems.append(f"A VAT line has no rate or taxable amount: {row}.")
            continue
        category = str(row.get("category") or ("S" if rate else "Z")).upper()
        rows.append((category, rate, taxable, amount if amount is not None else Decimal(0)))
    if not rows and net is not None and vat is not None:  # a one-rate invoice
        rate = (vat / net * 100).quantize(Decimal(1)) if net else Decimal(0)
        rows.append(("S" if rate else "Z", rate, net, vat))
    if rows:
        vat_sum = sum((r[3] for r in rows), Decimal(0))
        net_sum = sum((r[2] for r in rows), Decimal(0))
        net = net if net is not None else net_sum
        vat = vat if vat is not None else vat_sum
        if vat_sum != vat:
            problems.append(f"VAT lines add up to {vat_sum}, the total says {vat}.")
        if net_sum != net:
            problems.append(f"Taxable amounts add up to {net_sum}, net is {net}.")
    if net is None or vat is None:
        problems.append("No net amount and VAT were read.")
    elif gross is not None and net + vat != gross:
        problems.append(f"Net {net} + VAT {vat} = {net + vat}, but the total is {gross}.")
    allowed = allowed_vat_rates(issued) if issued else set()
    for category, rate, taxable, amount in rows:
        if category != "AE" and abs(taxable * rate / 100 - amount) > _TOLERANCE:
            problems.append(f"{rate}% of {taxable} isn't {amount}.")
        if issued and rate and category != "AE" and rate not in allowed:
            problems.append(f"{_rate(rate)}% VAT wasn't in force on {issued} "
                            f"(then: {', '.join(sorted(_rate(r) for r in allowed))}%).")  # fmt: skip

    kind = "credit_note" if str(facts.get("kind") or "").lower() == "credit_note" else "invoice"
    fields = {
        "source": "scan", "direction": direction, "invoice_number": number,
        "due_date": str(facts.get("due_date") or ""), "supplier_cui": supplier,
        "customer_cui": customer, "supplier_iban": str(facts.get("supplier_iban") or ""),
        "total_net": str(net) if net is not None else "",
        "total_vat": str(vat) if vat is not None else "",
        "vat_breakdown": [{"category": c, "rate": _rate(r), "taxable": str(t), "vat": str(v)}
                          for c, r, t, v in rows],
        "problems": problems,
    }  # fmt: skip
    total = gross if gross is not None else (net + vat if net is not None and vat is not None
                                             else None)  # fmt: skip
    return {
        "doc_type": kind,
        "amount": str(total) if total is not None else None,
        "currency": str(facts.get("currency") or "RON").upper(),
        "document_date": issued.isoformat() if issued else None,
        "sender": str(facts.get("supplier_name") or ""),
        "receiver": str(facts.get("customer_name") or ""),
        "fields": fields,
        "problems": problems,
    }
