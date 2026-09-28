"""Romanian working days: weekends, legal holidays (Codul muncii art. 139), Orthodox Easter."""

from __future__ import annotations

from datetime import date

import pytest

from langclaw.accounting.workdays import (
    is_working_day,
    next_working_day,
    orthodox_easter,
    public_holidays,
)


@pytest.mark.parametrize(
    ("year", "easter"),
    [(2024, date(2024, 5, 5)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 12)),
     (2027, date(2027, 5, 2))],
)  # fmt: skip
def test_orthodox_easter(year: int, easter: date) -> None:
    assert orthodox_easter(year) == easter


def test_public_holidays_2026() -> None:
    days = public_holidays(2026)
    assert days[date(2026, 1, 6)] == "Epiphany"
    assert days[date(2026, 1, 7)] == "Saint John the Baptist"
    assert days[date(2026, 4, 10)] == "Orthodox Good Friday"
    assert days[date(2026, 4, 13)] == "Orthodox Easter Monday"
    assert days[date(2026, 6, 1)] in {"Children's Day", "Orthodox Pentecost Monday"}
    assert date(2026, 5, 31) in days  # Pentecost Sunday
    assert {date(2026, 8, 15), date(2026, 11, 30), date(2026, 12, 1)} <= set(days)
    assert len(set(days)) == 16  # 17 holidays, two on 1 June


def test_epiphany_and_saint_john_only_from_2024() -> None:
    assert date(2023, 1, 6) not in public_holidays(2023)
    assert date(2024, 1, 7) in public_holidays(2024)


def test_next_working_day_skips_weekends_and_holidays() -> None:
    assert is_working_day(date(2026, 10, 26))
    assert not is_working_day(date(2026, 10, 25))  # Sunday
    assert next_working_day(date(2026, 10, 25)) == date(2026, 10, 26)
    assert next_working_day(date(2026, 12, 25)) == date(2026, 12, 28)
    assert next_working_day(date(2026, 10, 26)) == date(2026, 10, 26)
