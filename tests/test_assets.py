"""Fixed assets: linear depreciation schedule and the monthly entry."""

from __future__ import annotations

from decimal import Decimal

import pytest

from langclaw.accounting.assets import (
    Asset,
    depreciation_account,
    depreciation_entry,
    monthly_depreciation,
)

D = Decimal
LAPTOP = Asset(id=1, name="Laptop", account="2131", value=D("10000.00"),
               in_service="2026-01-20", life_months=36)  # fmt: skip


def test_depreciation_starts_the_month_after_it_goes_into_service() -> None:
    assert monthly_depreciation(LAPTOP, "2026-01") == D("0")
    assert monthly_depreciation(LAPTOP, "2026-02") == D("277.78")
    assert monthly_depreciation(LAPTOP, "2025-12") == D("0")


def test_the_last_month_takes_the_rounding_and_then_it_stops() -> None:
    # Feb 2026 is month 1 → month 36 is Jan 2029
    assert monthly_depreciation(LAPTOP, "2029-01") == D("10000.00") - 35 * D("277.78")
    assert monthly_depreciation(LAPTOP, "2029-02") == D("0")


def test_accumulated_accounts() -> None:
    assert depreciation_account("2131") == "2813"
    assert depreciation_account("2133.01") == "2813"
    assert depreciation_account("214") == "2814"
    assert depreciation_account("205") == "2805"
    with pytest.raises(ValueError, match="2xx"):
        depreciation_account("371")


def test_the_entry_groups_by_accumulated_account() -> None:
    car = Asset(id=2, name="Car", account="2133", value=D("60000"), in_service="2025-06-10",
                life_months=60)  # fmt: skip
    chair = Asset(id=3, name="Chairs", account="214", value=D("2400"), in_service="2025-12-01",
                  life_months=24)  # fmt: skip
    entry = depreciation_entry([LAPTOP, car, chair], "2026-03")
    lines = [(x["account"], x["debit"], x["credit"]) for x in entry["lines"]]
    assert lines == [
        ("6811", "1377.78", "0"),
        ("2813", "0", "1277.78"),
        ("2814", "0", "100.00"),
    ]
    assert depreciation_entry([LAPTOP], "2026-01") is None
