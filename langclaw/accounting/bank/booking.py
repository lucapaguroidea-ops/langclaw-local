"""
Journal entries for bank movements — deterministic, no model.

- A payment against an invoice: money in → D bank / C the customer account the
  invoice was booked on (4111 by default); money out → D the supplier account
  (401 by default) / C bank.
- A bank fee (a debit described as a commission / bank charge) → D 627 / C bank.

The bank account is 5121 (RON) or 5124 (foreign currency), unless the client's
profile maps the IBAN (``"bank_accounts": {"RO49...": "5121.01"}``).
"""

from __future__ import annotations

import re
from typing import Any

from langclaw.accounting.checks import CUSTOMER_ACCOUNTS, SUPPLIER_ACCOUNTS

_FEE = re.compile(r"comision|taxa\s+bancar|tax[aă]\s+administrare|bank\s+(fee|charge)", re.I)
FEE_ACCOUNT = "627"


def is_bank_fee(description: str) -> bool:
    return bool(_FEE.search(description or ""))


def bank_account(iban: str, currency: str, profile: dict[str, Any]) -> str:
    """The ledger account for the client's bank account *iban*."""
    mapped = (profile.get("bank_accounts") or {}).get(iban)
    if mapped:
        return str(mapped)
    return "5121" if (currency or "RON").upper() == "RON" else "5124"


def _partner_account(direction: str, invoice_lines: list[dict[str, Any]] | None) -> str:
    """The partner account the invoice was booked on, or the default."""
    accounts = CUSTOMER_ACCOUNTS if direction == "out" else SUPPLIER_ACCOUNTS
    side = "debit" if direction == "out" else "credit"
    for line in invoice_lines or []:
        account = str(line.get("account", ""))
        if account.split(".")[0] in accounts and float(line.get(side) or 0) > 0:
            return account
    return "4111" if direction == "out" else "401"


def payment_entry(
    invoice: dict[str, Any],
    amount: str,
    *,
    invoice_lines: list[dict[str, Any]] | None,
    bank: str,
) -> dict[str, Any]:
    """The entry for paying *amount* of *invoice* through *bank*."""
    f = invoice.get("fields") or {}
    direction = f.get("direction", "in")
    partner = _partner_account(direction, invoice_lines)
    number = f.get("invoice_number", "")
    if direction == "out":
        note = f"Încasare {number}".strip()
        debit, credit = bank, partner
    else:
        note = f"Plată {number}".strip()
        debit, credit = partner, bank
    return {
        "lines": [
            {"account": debit, "debit": amount, "credit": "0", "explanation": note},
            {"account": credit, "debit": "0", "credit": amount, "explanation": note},
        ],
        "reasoning": f"{note}: extras de cont.",
        "legal_basis": "OMFP 1802/2014",
    }


def fee_entry(amount: str, *, bank: str) -> dict[str, Any]:
    return {
        "lines": [
            {"account": FEE_ACCOUNT, "debit": amount, "credit": "0",
             "explanation": "Comision bancar"},
            {"account": bank, "debit": "0", "credit": amount, "explanation": "Comision bancar"},
        ],
        "reasoning": "Comision bancar din extras.",
        "legal_basis": "OMFP 1802/2014",
    }  # fmt: skip


_DEPOSIT = re.compile(r"depunere\s+numerar|depunere\s+cash|cash\s+deposit", re.I)
_WITHDRAWAL = re.compile(r"retragere\s+numerar|ridicare\s+numerar|retragere\s+atm|"
                         r"cash\s+withdrawal", re.I)  # fmt: skip
TRANSFER_ACCOUNT, CASH_ACCOUNT = "581", "5311"


def cash_transfer(description: str) -> str | None:
    """``"deposit"`` / ``"withdrawal"`` when the movement moves cash in or out of the
    bank account, else ``None``."""
    if _DEPOSIT.search(description or ""):
        return "deposit"
    if _WITHDRAWAL.search(description or ""):
        return "withdrawal"
    return None


def cash_transfer_entry(kind: str, amount: str, *, bank: str) -> dict[str, Any]:
    """Cash moved between the register and the bank, through 581 (viramente interne)."""
    source, target = (CASH_ACCOUNT, bank) if kind == "deposit" else (bank, CASH_ACCOUNT)
    note = "Depunere numerar" if kind == "deposit" else "Ridicare numerar"

    def line(account: str, debit: str, credit: str) -> dict[str, str]:
        return {"account": account, "debit": debit, "credit": credit, "explanation": note}

    return {
        "lines": [
            line(TRANSFER_ACCOUNT, amount, "0"),
            line(source, "0", amount),
            line(target, amount, "0"),
            line(TRANSFER_ACCOUNT, "0", amount),
        ],
        "reasoning": f"{note} prin 581.",
        "legal_basis": "OMFP 1802/2014",
    }
