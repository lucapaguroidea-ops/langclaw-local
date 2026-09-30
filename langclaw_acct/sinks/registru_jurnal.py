"""
Registru Jurnal exports from the client's accounting system — the witness the
graphs read before Mouth-0 (00_LAW §8).

One reader per system in :data:`RJ_READERS`. A system's layout is defined from
a real export of that system and nothing else: until one has been given, its
entry is absent and asking for it says so. ``generic_csv`` is the synthetic
layout used by tests and as the shape every reader returns.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from langclaw_acct.types import FiscalDate, Money


class RjLine(BaseModel):
    """One line of a Registru Jurnal: debit account, credit account, amount."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: FiscalDate
    doc_number: str
    doc_type: str
    explanation: str
    debit: str
    credit: str
    amount: Money
    partner_cui: str | None = None


class RjFormatError(ValueError):
    """The export doesn't have the layout its reader expects."""


GENERIC_COLUMNS = ("data", "nr_document", "fel_document", "explicatie",
                   "cont_debit", "cont_credit", "suma")  # fmt: skip


def _amount(raw: str, where: str) -> str:
    text = raw.strip().replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")  # 1.234,50
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return str(Decimal(text).quantize(Decimal("0.01")))
    except InvalidOperation:
        raise RjFormatError(f"{where}: amount {raw!r} is not a number") from None


def read_generic_csv(data: bytes) -> list[RjLine]:
    """The synthetic layout: a header with :data:`GENERIC_COLUMNS`, ISO dates."""
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    missing = set(GENERIC_COLUMNS) - set(rows.fieldnames or ())
    if missing:
        raise RjFormatError(f"generic_csv: missing columns {sorted(missing)}")
    out: list[RjLine] = []
    for n, r in enumerate(rows, start=2):
        out.append(RjLine(
            date=r["data"].strip(), doc_number=r["nr_document"].strip(),
            doc_type=r["fel_document"].strip(), explanation=r["explicatie"].strip(),
            debit=r["cont_debit"].strip(), credit=r["cont_credit"].strip(),
            amount=_amount(r["suma"], f"line {n}"),
            partner_cui=(r.get("cui_partener") or "").strip() or None,
        ))  # fmt: skip
    return out


#: system → reader. SAGA / NextUp / SAP join when a real export of each is in.
RJ_READERS: dict[str, Callable[[bytes], list[RjLine]]] = {
    "generic_csv": read_generic_csv,
}


def read_registru_jurnal(system: str, data: bytes) -> list[RjLine]:
    """Parse a Registru Jurnal export of *system*.

    Raises:
        RjFormatError: no reader for *system* yet, or the file doesn't match it.
    """
    reader = RJ_READERS.get(system)
    if reader is None:
        raise RjFormatError(
            f"No Registru Jurnal layout for {system!r} yet (have: {sorted(RJ_READERS)}). "
            "It is defined from a real export of that system — send one."
        )
    return reader(data)
