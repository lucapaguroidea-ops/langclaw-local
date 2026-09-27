"""Period close (RO): trial balance, VAT summary, blockers, locking."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from langclaw.accounting.period import blockers, parse_period, trial_balance, vat_summary

D = Decimal


def test_parse_period() -> None:
    assert parse_period("2026-09") == (date(2026, 9, 1), date(2026, 9, 30))
    assert parse_period("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    for bad in ("2026-13", "sept", "2026/09", ""):
        with pytest.raises(ValueError, match="YYYY-MM"):
            parse_period(bad)


def test_trial_balance_sums_per_account_and_balances() -> None:
    lines = [
        {"account": "628", "debit": 100, "credit": 0},
        {"account": "4426", "debit": 21, "credit": 0},
        {"account": "401", "debit": 0, "credit": 121},
        {"account": "628", "debit": 50, "credit": 0},
        {"account": "4426", "debit": 10.5, "credit": 0},
        {"account": "401", "debit": 0, "credit": 60.5},
    ]
    tb = trial_balance(lines)
    by = {r["account"]: r for r in tb["accounts"]}
    assert list(by) == ["401", "4426", "628"]
    assert by["628"] == {"account": "628", "debit": D("150.00"), "credit": D("0.00"),
                         "balance": D("150.00")}  # fmt: skip
    assert by["401"]["balance"] == D("-181.50")
    assert tb["debit"] == tb["credit"] == D("181.50") and tb["balanced"] is True


def _inv(direction, rate, taxable, vat, doc_type="invoice", category="S"):
    return {
        "doc_type": doc_type,
        "fields": {
            "direction": direction,
            "vat_breakdown": [{"category": category, "rate": rate, "taxable": taxable, "vat": vat}],
        },
    }


def test_vat_summary_groups_by_rate_and_nets_payable() -> None:
    s = vat_summary(
        [
            _inv("out", "21.00", "1000.00", "210.00"),
            _inv("out", "11.00", "100.00", "11.00"),
            _inv("out", "21.00", "-200.00", "-42.00", doc_type="credit_note"),
            _inv("in", "21.00", "500.00", "105.00"),
            _inv("in", "21.00", "100.00", "0", category="AE"),  # reverse charge
        ]
    )
    sales = {r["rate"]: r for r in s["sales"]}
    assert sales["21"]["taxable"] == D("800.00") and sales["21"]["vat"] == D("168.00")
    assert sales["11"]["vat"] == D("11.00")
    assert s["reverse_charge"] == {"taxable": D("100.00"), "vat": D("21.00")}
    assert s["collected"] == D("168.00") + D("11.00") + D("21.00")
    assert s["deductible"] == D("105.00") + D("21.00")
    assert s["payable"] == D("74.00") and s["refundable"] == D("0.00")


def test_credit_notes_with_positive_amounts_are_negated() -> None:
    s = vat_summary([_inv("out", "21.00", "200.00", "42.00", doc_type="credit_note")])
    assert s["sales"][0]["vat"] == D("-42.00") and s["refundable"] == D("42.00")


def test_blockers_are_invoices_without_an_entry() -> None:
    docs = [
        {"bucket_key": "a", "doc_type": "invoice", "status": "posted"},
        {"bucket_key": "b", "doc_type": "invoice", "status": "exported"},
        {"bucket_key": "c", "doc_type": "invoice", "status": "filed"},
        {"bucket_key": "d", "doc_type": "credit_note", "status": "needs_manual_entry"},
        {"bucket_key": "e", "doc_type": "contract", "status": "filed"},
    ]
    assert [b["bucket_key"] for b in blockers(docs)] == ["c", "d"]
