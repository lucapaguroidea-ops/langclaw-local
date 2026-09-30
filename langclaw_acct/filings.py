"""
WP-12 — filing items: what a client-period owes, as gates with receipts.

:func:`filings_for` opens one :class:`FilingItem` per ``ArticoleFiling`` row
whose ``require`` / ``forbid`` match the period's CO.DiT (an axis not set
opens nothing). This system never submits to ANAF and never builds the
declaration (SAGA does); an item closes only when its receipt is stored
(:meth:`FilingRegister.receipt`) — never because the date passed.

Due dates: the 25th of the next month (D394: the 30th, end of February for
January), moved past weekends and legal holidays. A form whose frequency
the CO.DiT doesn't say (D300 / D394 without ``tva_period``, D406) gets no
date and says why, rather than a guessed one.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

from langclaw.accounting.workdays import next_working_day  # reference data only
from langclaw_acct.catalog import Catalog
from langclaw_acct.codit import CoDit
from langclaw_acct.types import FilingItem

DAY = {"D394": 30}
QUARTERLY = {"D100"}
NEEDS_TVA_PERIOD = {"D300", "D394"}


class FilingError(ValueError):
    """Unknown item, or closing one without a receipt."""


@dataclass(frozen=True, slots=True)
class DueItem:
    item: FilingItem
    form: str
    due: str | None
    books_gate: tuple[str, ...]
    note: str = ""


def _due(form: str, period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    day = min(DAY.get(form, 25), calendar.monthrange(y, m)[1])
    return next_working_day(date(y, m, day)).isoformat()


def _matches(row: dict, codit: CoDit) -> bool:
    for axis, vals in (row.get("require") or {}).items():
        if codit.value(axis) not in vals:
            return False
    return not any(codit.value(a) in vals for a, vals in (row.get("forbid") or {}).items())


def filings_for(catalog: Catalog, codit: CoDit) -> list[DueItem]:
    month = int(codit.period[5:7])
    out: list[DueItem] = []
    for fid, row in catalog.rows["ArticoleFiling"].items():
        if not _matches(row, codit):
            continue
        form, note, due = row["form"], "", None
        if form in QUARTERLY and month % 3:
            continue
        if form in NEEDS_TVA_PERIOD:
            tp = codit.value("tva_period")
            if tp == "quarterly" and month % 3:
                continue
            if tp in ("monthly", "quarterly"):
                due = _due(form, codit.period)
            else:
                note = "tva_period not set on the CO.DiT: frequency unknown"
        elif form == "D406":
            note = "D406 frequency depends on the company's size category; not derived yet"
        else:
            due = _due(form, codit.period)
        item = FilingItem(filing_id=fid, period=codit.period)
        out.append(DueItem(item, form, due, tuple(row.get("books_gate") or ()), note))
    return out


@dataclass
class FilingRegister:
    """Open items per ``(cui, period, filing_id)``; a receipt closes one."""

    items: dict[tuple[str, str, str], FilingItem] = field(default_factory=dict)
    receipts: dict[tuple[str, str, str], dict[str, str]] = field(default_factory=dict)

    def open(self, cui: str, due: list[DueItem]) -> None:
        for d in due:
            self.items.setdefault((cui, d.item.period, d.item.filing_id), d.item)

    def receipt(
        self, cui: str, period: str, filing_id: str, *, receipt_key: str, by: str
    ) -> FilingItem:
        key = (cui, period, filing_id)
        if key not in self.items:
            raise FilingError(f"no open filing {filing_id} for {period}")
        if not receipt_key.strip():
            raise FilingError("a filing closes with its receipt, not without one")
        item = self.items[key].model_copy(update={"state": "filed", "receipt_key": receipt_key})
        self.items[key] = item
        self.receipts[key] = {"receipt_key": receipt_key, "by": by}
        return item

    def open_items(self, cui: str) -> list[FilingItem]:
        return [i for (c, _, _), i in self.items.items() if c == cui and i.state == "open"]
