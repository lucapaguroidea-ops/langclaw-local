"""
Romanian working days — for tax deadlines that fall on a day off.

Codul de procedură fiscală art. 181: a deadline that ends on a non-working day
moves to the next working day. Non-working days are Saturdays, Sundays and the
legal holidays of Codul muncii art. 139, some of which follow Orthodox Easter.

The holiday list is reference data — add a row when the law changes.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

#: Fixed-date legal holidays: (month, day, name, first year it applies).
FIXED_HOLIDAYS: tuple[tuple[int, int, str, int], ...] = (
    (1, 1, "New Year's Day", 0),
    (1, 2, "New Year's Day (second day)", 0),
    (1, 6, "Epiphany", 2024),
    (1, 7, "Saint John the Baptist", 2024),
    (1, 24, "Union Day", 0),
    (5, 1, "Labour Day", 0),
    (6, 1, "Children's Day", 0),
    (8, 15, "Dormition of the Mother of God", 0),
    (11, 30, "Saint Andrew", 0),
    (12, 1, "National Day", 0),
    (12, 25, "Christmas", 0),
    (12, 26, "Christmas (second day)", 0),
)
#: Holidays that follow Orthodox Easter: (days from Easter Sunday, name).
EASTER_HOLIDAYS: tuple[tuple[int, str], ...] = (
    (-2, "Orthodox Good Friday"),
    (0, "Orthodox Easter"),
    (1, "Orthodox Easter Monday"),
    (49, "Orthodox Pentecost"),
    (50, "Orthodox Pentecost Monday"),
)


def orthodox_easter(year: int) -> date:
    """Orthodox Easter Sunday (Gregorian date) — Meeus' Julian algorithm, 1900–2099."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(year, month, day + 1) + timedelta(days=13)


@cache
def public_holidays(year: int) -> dict[date, str]:
    """The legal holidays of *year*, by date (a day with two holidays keeps the first)."""
    days: dict[date, str] = {}
    for month, day, name, since in FIXED_HOLIDAYS:
        if year >= since:
            days.setdefault(date(year, month, day), name)
    easter = orthodox_easter(year)
    for offset, name in EASTER_HOLIDAYS:
        days.setdefault(easter + timedelta(days=offset), name)
    return dict(sorted(days.items()))


def day_off(day: date) -> str | None:
    """Why *day* is not a working day (``"Saturday"``, a holiday's name), or ``None``."""
    if day.weekday() >= 5:
        return day.strftime("%A")
    return public_holidays(day.year).get(day)


def is_working_day(day: date) -> bool:
    return day_off(day) is None


def next_working_day(day: date) -> date:
    """*day* itself when it's a working day, else the first working day after it."""
    while not is_working_day(day):
        day += timedelta(days=1)
    return day
