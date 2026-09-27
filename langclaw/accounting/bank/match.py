"""Match bank movements to the invoices they pay."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from langclaw.accounting.bank.parse import Transaction

_CENT = Decimal("0.01")
_LEGAL = {"srl", "sa", "sc", "pfa", "ii", "if", "snc", "srl-d", "co"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _name_matches(name: str, text: str) -> bool:
    words = {w for w in _words(name) if w not in _LEGAL}
    return bool(words) and any(len(w) >= 3 for w in words) and words <= _words(text)


def _number_matches(number: str, text: str) -> bool:
    number = number.strip()
    if len(number) < 2:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(number)}(?![A-Za-z0-9])", text, re.I))


def match_payments(
    transactions: list[Transaction], invoices: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """``{"key", "reference", "bucket_key", "kind": certain|probable, "because"}``
    per match (``key`` is the movement's :attr:`Transaction.key`).

    Money in can only pay a sales invoice, money out a purchase invoice; the
    amount must equal the invoice's gross. Each invoice is used once.
    """
    used: set[str] = set()
    out = []
    for tx in transactions:
        direction = "out" if tx.amount > 0 else "in"
        text = f"{tx.counterparty} {tx.description}"
        candidates = [
            inv
            for inv in invoices
            if inv["bucket_key"] not in used
            and inv.get("doc_type", "invoice") == "invoice"
            and (inv.get("fields") or {}).get("direction") == direction
            and Decimal(str(inv.get("amount") or 0)).quantize(_CENT) == abs(tx.amount)
        ]
        signalled = []
        for inv in candidates:
            f = inv.get("fields") or {}
            partner = inv.get("sender" if direction == "in" else "receiver") or ""
            if _number_matches(f.get("invoice_number", ""), text):
                signalled.append((inv, "amount + invoice number"))
            elif tx.iban and tx.iban == f.get("supplier_iban"):
                signalled.append((inv, "amount + IBAN"))
            elif _name_matches(partner, text):
                signalled.append((inv, "amount + partner name"))
        if len(signalled) == 1:
            (inv, because), kind = signalled[0], "certain"
        elif not signalled and len(candidates) == 1:
            inv, because, kind = candidates[0], "amount only", "probable"
        else:
            continue
        used.add(inv["bucket_key"])
        out.append(
            {"key": tx.key, "reference": tx.reference, "bucket_key": inv["bucket_key"],
             "kind": kind, "because": because}
        )  # fmt: skip
    return out
