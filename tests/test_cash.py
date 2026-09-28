"""Daily cash register (raport Z) entries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from langclaw.accounting.cash import z_report_entry

D = Decimal


def test_a_z_report_books_cash_revenue_and_vat_by_rate() -> None:
    entry = z_report_entry(date(2026, 9, 15), [{"rate": 21, "gross": 1210},
                                               {"rate": 11, "gross": "111.00"}])  # fmt: skip
    lines = [(x["account"], x["debit"], x["credit"]) for x in entry["lines"]]
    assert lines == [
        ("5311", "1321.00", "0"),
        ("707", "0", "1100.00"),
        ("4427", "0", "221.00"),
    ]
    assert entry["totals"] == {"gross": D("1321.00"), "net": D("1100.00"), "vat": D("221.00")}


def test_revenue_account_and_zero_rate() -> None:
    entry = z_report_entry(date(2026, 9, 15), [{"rate": 0, "gross": 50}], revenue_account="704")
    assert [(x["account"], x["credit"]) for x in entry["lines"][1:]] == [("704", "50.00")]


def test_rates_must_be_valid_on_the_day() -> None:
    with pytest.raises(ValueError, match="19"):
        z_report_entry(date(2026, 9, 15), [{"rate": 19, "gross": 119}])
    with pytest.raises(ValueError, match="empty"):
        z_report_entry(date(2026, 9, 15), [])
