"""Daily cash register (raport Z) entries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from langclaw.accounting.cash import cash_book, cash_expense_entry, z_report_entry

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


def test_the_cash_book_runs_day_by_day_and_flags_a_negative_balance() -> None:
    lines = [
        {"entry_date": date(2026, 9, 2), "bucket_key": "cash/z/2026-09-02",
         "debit": D("300"), "credit": D("0"), "explanation": "Raport Z"},
        {"entry_date": date(2026, 9, 3), "bucket_key": "bank/t1/cash",
         "debit": D("0"), "credit": D("500"), "explanation": "depunere"},
        {"entry_date": date(2026, 9, 4), "bucket_key": "cash/z/2026-09-04",
         "debit": D("400"), "credit": D("0"), "explanation": "Raport Z"},
    ]  # fmt: skip
    book = cash_book(D("100"), lines)
    assert [(d["day"], d["receipts"], d["payments"], d["closing"]) for d in book["days"]] == [
        ("2026-09-02", "300.00", "0.00", "400.00"),
        ("2026-09-03", "0.00", "500.00", "-100.00"),
        ("2026-09-04", "400.00", "0.00", "300.00"),
    ]
    assert book["opening"] == "100.00" and book["closing"] == "300.00"
    assert [p["day"] for p in book["problems"]] == ["2026-09-03"]
    assert "negative" in book["problems"][0]["problem"]


def test_a_cash_limit_is_checked_only_when_set() -> None:
    lines = [{"entry_date": date(2026, 9, 2), "bucket_key": "z", "debit": D("900"),
              "credit": D("0"), "explanation": ""}]  # fmt: skip
    assert cash_book(D("0"), lines)["problems"] == []
    problems = cash_book(D("0"), lines, limit=D("500"))["problems"]
    assert problems and "500.00" in problems[0]["problem"]


def test_a_cash_receipt_books_the_expense_and_deductible_vat() -> None:
    entry = cash_expense_entry(date(2026, 9, 15), "121", 21, "6022", deduct_vat=True)
    assert [(x["account"], x["debit"], x["credit"]) for x in entry["lines"]] == [
        ("6022", "100.00", "0"), ("4426", "21.00", "0"), ("5311", "0", "121.00")]  # fmt: skip


def test_without_deduction_the_vat_is_part_of_the_expense() -> None:
    entry = cash_expense_entry(date(2026, 9, 15), "121", 21, "6022", deduct_vat=False)
    assert [(x["account"], x["debit"]) for x in entry["lines"][:-1]] == [("6022", "121.00")]


def test_cash_receipts_need_an_expense_account_and_a_valid_rate() -> None:
    with pytest.raises(ValueError, match="4111"):
        cash_expense_entry(date(2026, 9, 15), "121", 21, "4111", deduct_vat=True)
    with pytest.raises(ValueError, match="19"):
        cash_expense_entry(date(2026, 9, 15), "119", 19, "6022", deduct_vat=True)
    with pytest.raises(ValueError, match="above 0"):
        cash_expense_entry(date(2026, 9, 15), "0", 21, "6022", deduct_vat=True)
