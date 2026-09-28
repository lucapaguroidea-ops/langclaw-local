"""
Account ledger (fișa contului) — one account's movements with a running balance.

Pure function over journal lines (:meth:`Journal.account_lines`) and the balance
before the first of them (:meth:`Journal.balance_until`). Balances are debit −
credit, so a negative figure is a credit balance (e.g. 401 suppliers, 4427 VAT).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

_CENT = Decimal("0.01")
_ACCOUNT = re.compile(r"^\d{3,4}(\.\w+)?$")


def account_ledger(account: str, opening: Decimal, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """*account*'s ledger from *opening* and its journal *lines* (``entry_date``,
    ``bucket_key``, ``debit``, ``credit``, ``explanation``), in date order.

    Raises:
        ValueError: *account* isn't an account number (``4111``, ``5121.01``).
    """
    if not _ACCOUNT.match(account or ""):
        raise ValueError(f"{account!r} isn't an account number (e.g. 4111 or 5121.01).")
    balance = Decimal(opening).quantize(_CENT)
    debit = credit = Decimal(0)
    rows = []
    for line in sorted(lines, key=lambda x: x["entry_date"]):
        d, c = Decimal(line["debit"]), Decimal(line["credit"])
        debit, credit = debit + d, credit + c
        balance = (balance + d - c).quantize(_CENT)
        rows.append({"date": line["entry_date"].isoformat(), "document": line["bucket_key"],
                     "explanation": line.get("explanation") or "", "debit": f"{d:.2f}",
                     "credit": f"{c:.2f}", "balance": str(balance)})  # fmt: skip
    return {
        "account": account,
        "opening": str(Decimal(opening).quantize(_CENT)),
        "debit": f"{debit:.2f}",
        "credit": f"{credit:.2f}",
        "closing": str(balance),
        "side": "credit" if balance < 0 else "debit",
        "lines": rows,
    }
