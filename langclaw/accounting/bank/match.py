"""Match bank movements to the invoices they pay (whole, several, or in part)."""

from __future__ import annotations

import re
from decimal import Decimal
from itertools import combinations
from typing import Any

from langclaw.accounting.bank.parse import Transaction

_CENT = Decimal("0.01")
_LEGAL = {"srl", "sa", "sc", "pfa", "ii", "if", "snc", "srl-d", "co"}
#: Most invoices of one partner searched for a combination that adds up.
MAX_GROUP_SEARCH = 12


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


def outstanding(invoice: dict[str, Any]) -> Decimal:
    """What's left to pay on *invoice*: its total minus ``fields.paid_amount``."""
    gross = Decimal(str(invoice.get("amount") or 0)).quantize(_CENT)
    paid = Decimal(str((invoice.get("fields") or {}).get("paid_amount") or 0)).quantize(_CENT)
    return gross - paid


def match_payments(
    transactions: list[Transaction], invoices: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One result per matched movement::

        {"key", "reference", "kind": certain|probable|partial, "because",
         "bucket_key", "allocations": [{"bucket_key", "amount"}]}

    Money in only pays sales invoices, money out only purchases. In order:

    1. one invoice whose outstanding equals the amount, with a second signal
       (its number in the description, the supplier's IBAN, the partner's name)
       → ``certain``;
    2. several invoices all named in the description whose outstanding adds up
       → ``certain``;
    3. exactly one set of the partner's open invoices that adds up → ``certain``;
    4. one invoice named in the description with more outstanding than the
       amount → ``partial`` (the amount goes to it);
    5. a single invoice with the right amount and no other signal → ``probable``.

    Anything else (several candidates, no signal) isn't matched.
    """
    left = {inv["bucket_key"]: outstanding(inv) for inv in invoices}
    out = []
    for tx in transactions:
        direction = "out" if tx.amount > 0 else "in"
        amount = abs(tx.amount).quantize(_CENT)
        text = f"{tx.counterparty} {tx.description}"
        open_ = [
            inv
            for inv in invoices
            if inv.get("doc_type", "invoice") == "invoice"
            and (inv.get("fields") or {}).get("direction") == direction
            and left[inv["bucket_key"]] > 0
        ]

        def named(inv: dict[str, Any]) -> bool:
            return _number_matches((inv.get("fields") or {}).get("invoice_number", ""), text)

        def partner(inv: dict[str, Any], direction: str = direction) -> bool:
            f = inv.get("fields") or {}
            if tx.iban and tx.iban == f.get("supplier_iban"):
                return True
            return _name_matches(inv.get("sender" if direction == "in" else "receiver") or "", text)

        allocations: list[tuple[dict[str, Any], Decimal]] = []
        kind = because = ""
        exact = [inv for inv in open_ if left[inv["bucket_key"]] == amount]
        signalled = [inv for inv in exact if named(inv) or partner(inv)]
        by_name = [inv for inv in open_ if named(inv)]
        if len(signalled) == 1:
            inv = signalled[0]
            reason = "invoice number" if named(inv) else (
                "IBAN" if tx.iban and tx.iban == (inv.get("fields") or {}).get("supplier_iban")
                else "partner name")  # fmt: skip
            allocations, kind, because = [(inv, amount)], "certain", f"amount + {reason}"
        elif len(by_name) >= 2 and sum(left[i["bucket_key"]] for i in by_name) == amount:
            allocations = [(i, left[i["bucket_key"]]) for i in by_name]
            kind, because = "certain", "sum of invoices named"
        elif not signalled and (group := _unique_group([i for i in open_ if partner(i)], left,
                                                       amount)):  # fmt: skip
            allocations = [(i, left[i["bucket_key"]]) for i in group]
            kind, because = "certain", "sum of the partner's open invoices"
        elif len(by_name) == 1 and left[by_name[0]["bucket_key"]] > amount:
            allocations, kind, because = [(by_name[0], amount)], "partial", "invoice number"
        elif not signalled and len(exact) == 1:
            allocations, kind, because = [(exact[0], amount)], "probable", "amount only"
        if not allocations:
            continue
        for inv, part in allocations:
            left[inv["bucket_key"]] -= part
        out.append(
            {
                "key": tx.key,
                "reference": tx.reference,
                "bucket_key": allocations[0][0]["bucket_key"],
                "kind": kind,
                "because": because,
                "allocations": [
                    {"bucket_key": inv["bucket_key"], "amount": str(part)}
                    for inv, part in allocations
                ],
            }
        )
    return out


def _unique_group(
    invoices: list[dict[str, Any]], left: dict[str, Decimal], amount: Decimal
) -> list[dict[str, Any]] | None:
    """The one set (2+ invoices) whose outstanding adds up to *amount*, else ``None``."""
    if not 2 <= len(invoices) <= MAX_GROUP_SEARCH:
        return None
    found = None
    for size in range(2, len(invoices) + 1):
        for combo in combinations(invoices, size):
            if sum(left[i["bucket_key"]] for i in combo) == amount:
                if found is not None:
                    return None
                found = list(combo)
    return found
