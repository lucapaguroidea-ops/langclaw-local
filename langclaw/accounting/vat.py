"""
Romanian VAT rates by date — reference data, not code paths.

Rates change (Law 141/2025 moved the standard rate from 19% to 21% and merged
the reduced 5% / 9% into 11% from 1 August 2025, with a transitional 9% for some
housing deliveries). Checks ask "which rates were valid on this invoice's date",
never "is it 19%". Have your accountant review this table; add a row when the
law changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RatePeriod:
    start: date
    end: date | None  # inclusive; None = still in force
    rates: frozenset[Decimal]
    note: str = ""


RO_VAT_PERIODS: tuple[RatePeriod, ...] = (
    RatePeriod(date(2017, 1, 1), date(2025, 7, 31), frozenset(map(Decimal, (19, 9, 5, 0)))),
    RatePeriod(date(2025, 8, 1), None, frozenset(map(Decimal, (21, 11, 0)))),
    RatePeriod(
        date(2025, 8, 1),
        date(2026, 7, 31),
        frozenset({Decimal(9)}),
        note="transitional 9% for certain housing deliveries",
    ),
)


def allowed_vat_rates(on: date, periods: tuple[RatePeriod, ...] = RO_VAT_PERIODS) -> set[Decimal]:
    """Every VAT rate (percent) in force on *on*."""
    return {
        rate
        for p in periods
        if p.start <= on and (p.end is None or on <= p.end)
        for rate in p.rates
    }
