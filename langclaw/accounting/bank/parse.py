"""MT940 and CAMT.053 statement parsers (the formats Romanian banks export)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

_CENT = Decimal("0.01")
_IBAN = re.compile(r"\bRO\d{2}[A-Z]{4}[A-Z0-9]{16}\b")


class BankStatementError(ValueError):
    """The file isn't a readable bank statement."""


@dataclass(slots=True)
class Transaction:
    booked: str  # ISO date
    amount: Decimal  # + money in, − money out
    currency: str
    reference: str = ""
    counterparty: str = ""
    iban: str = ""
    """The counterparty's IBAN, when the bank gives it."""
    description: str = ""

    @property
    def key(self) -> str:
        """Stable id for idempotent imports (the same movement imports once)."""
        raw = f"{self.booked}|{self.amount}|{self.reference}|{self.description}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass(slots=True)
class Statement:
    format: str
    iban: str
    currency: str
    opening: Decimal
    closing: Decimal
    date_from: str
    date_to: str
    transactions: list[Transaction] = field(default_factory=list)

    def check(self) -> list[str]:
        """Problems (empty when opening + movements = closing)."""
        total = (self.opening + sum((t.amount for t in self.transactions), Decimal(0))).quantize(
            _CENT
        )
        if total != self.closing:
            return [
                f"Opening {self.opening} + movements = {total}, but the closing balance "
                f"is {self.closing}."
            ]
        return []


def parse_statement(data: bytes) -> Statement:
    """Parse an MT940 or CAMT.053 statement (detected from the content).

    Raises:
        BankStatementError: neither format, or unreadable.
    """
    text = data.decode("utf-8", errors="replace").lstrip("﻿ \r\n\t")
    try:
        if text.startswith("<") and "camt.053" in text[:2000]:
            return _camt(data)
        if ":60F:" in text or ":60M:" in text:
            return _mt940(text)
    except (InvalidOperation, ET.ParseError, IndexError, AttributeError, ValueError) as exc:
        raise BankStatementError(f"Couldn't read the statement: {exc}") from exc
    raise BankStatementError("Not a bank statement: expected MT940 or CAMT.053.")


# -- MT940 ---------------------------------------------------------------------------------

_TAG = re.compile(r"^:(\w{2,3}):", re.M)
_BALANCE = re.compile(r"^([CD])(\d{6})([A-Z]{3})([\d,]+)")
_LINE = re.compile(r"^(\d{6})(\d{4})?(R?[CD])[A-Z]?([\d,]+)N?(\w{3})?([^/\n]*)(?://(\S+))?")


def _mt_amount(value: str) -> Decimal:
    return Decimal(value.replace(",", ".")).quantize(_CENT)


def _mt_date(yymmdd: str) -> str:
    return f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"


def _mt940(text: str) -> Statement:
    parts = _TAG.split(text)
    tags = [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]
    iban, currency = "", ""
    opening = closing = Decimal(0)
    date_from = date_to = ""
    txs: list[Transaction] = []
    for tag, value in tags:
        if tag == "25":
            iban = value.split("/")[-1].replace(" ", "")
        elif tag in ("60F", "60M", "62F", "62M"):
            m = _BALANCE.match(value)
            if not m:
                raise ValueError(f"bad balance :{tag}:{value}")
            amount = _mt_amount(m[4]) * (-1 if m[1] == "D" else 1)
            currency = currency or m[3]
            if tag.startswith("60") and not date_from:
                opening, date_from = amount, _mt_date(m[2])
            elif tag.startswith("62"):
                closing, date_to = amount, _mt_date(m[2])
        elif tag == "61":
            m = _LINE.match(value)
            if not m:
                raise ValueError(f"bad movement :61:{value}")
            sign = -1 if m[3].endswith("D") else 1
            own_ref = (m[6] or "").strip()
            ref = (m[7] or "").strip() or ("" if own_ref == "NONREF" else own_ref)
            txs.append(
                Transaction(_mt_date(m[1]), sign * _mt_amount(m[4]), currency, reference=ref)
            )
        elif tag == "86" and txs:
            desc = " ".join(value.split())
            txs[-1].description = desc
            if found := _IBAN.search(desc):
                txs[-1].iban = found[0]
    for t in txs:
        t.currency = currency
    return Statement("mt940", iban, currency, opening, closing, date_from, date_to, txs)


# -- CAMT.053 ------------------------------------------------------------------------------


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(node: ET.Element | None, path: str) -> str:
    found = node.find(path) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def _remittance(tx: ET.Element | None) -> str:
    lines = [u.text or "" for u in tx.findall("RmtInf/Ustrd")] if tx is not None else []
    return " ".join(" ".join(lines).split())


def _camt(data: bytes) -> Statement:
    root = _strip_ns(ET.fromstring(data))
    stmt = root.find(".//Stmt")
    if stmt is None:
        raise ValueError("no <Stmt>")
    balances: dict[str, tuple[Decimal, str]] = {}
    for bal in stmt.findall("Bal"):
        code = _t(bal, "Tp/CdOrPrtry/Cd")
        amount = Decimal(_t(bal, "Amt")).quantize(_CENT)
        if _t(bal, "CdtDbtInd") == "DBIT":
            amount = -amount
        balances[code] = (amount, _t(bal, "Dt/Dt") or _t(bal, "Dt/DtTm")[:10])
    opening, date_from = balances.get("OPBD") or balances.get("PRCD") or (Decimal(0), "")
    closing, date_to = balances.get("CLBD") or (Decimal(0), "")
    first_amount = stmt.find("Bal/Amt")
    currency = _t(stmt, "Acct/Ccy") or (
        first_amount.get("Ccy", "") if first_amount is not None else ""
    )
    txs = []
    for entry in stmt.findall("Ntry"):
        credit = _t(entry, "CdtDbtInd") == "CRDT"
        amount = Decimal(_t(entry, "Amt")).quantize(_CENT)
        tx = entry.find("NtryDtls/TxDtls")
        party = "Dbtr" if credit else "Cdtr"
        txs.append(
            Transaction(
                booked=_t(entry, "BookgDt/Dt") or _t(entry, "BookgDt/DtTm")[:10],
                amount=amount if credit else -amount,
                currency=entry.find("Amt").get("Ccy", currency),
                reference=_t(entry, "AcctSvcrRef") or _t(tx, "Refs/EndToEndId"),
                counterparty=_t(tx, f"RltdPties/{party}/Nm"),
                iban=_t(tx, f"RltdPties/{party}Acct/Id/IBAN"),
                description=_remittance(tx),
            )
        )
    return Statement(
        "camt053", _t(stmt, "Acct/Id/IBAN"), currency, opening, closing, date_from, date_to, txs
    )
