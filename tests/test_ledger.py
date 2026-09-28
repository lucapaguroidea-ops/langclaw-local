"""The account ledger (fișa contului)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from langclaw.accounting.ledger import account_ledger

D = Decimal


def test_the_ledger_runs_a_balance_line_by_line() -> None:
    lines = [
        {"entry_date": date(2026, 9, 2), "bucket_key": "a", "debit": D("100"),
         "credit": D("0"), "explanation": "Factura 1", "counterparts": "704,4427"},
        {"entry_date": date(2026, 9, 5), "bucket_key": "b", "debit": D("0"),
         "credit": D("30"), "explanation": ""},
    ]  # fmt: skip
    ledger = account_ledger("4111", D("50"), lines)
    assert [(r["date"], r["debit"], r["credit"], r["balance"]) for r in ledger["lines"]] == [
        ("2026-09-02", "100.00", "0.00", "150.00"),
        ("2026-09-05", "0.00", "30.00", "120.00"),
    ]
    assert ledger["opening"] == "50.00" and ledger["closing"] == "120.00"
    assert ledger["debit"] == "100.00" and ledger["credit"] == "30.00"
    assert ledger["side"] == "debit"
    assert [r["counterpart"] for r in ledger["lines"]] == ["704,4427", ""]


def test_a_credit_balance_is_reported_as_such() -> None:
    ledger = account_ledger("401", D("0"), [{"entry_date": date(2026, 9, 2), "bucket_key": "x",
                                             "debit": D("0"), "credit": D("80"),
                                             "explanation": ""}])  # fmt: skip
    assert ledger["closing"] == "-80.00" and ledger["side"] == "credit"


def test_the_account_must_look_like_one() -> None:
    with pytest.raises(ValueError, match="account"):
        account_ledger("abc", D("0"), [])
