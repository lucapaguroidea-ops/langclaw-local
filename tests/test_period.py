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


def test_expected_documents_are_counted_per_type() -> None:
    from langclaw.accounting.period import document_state

    docs = [
        {"bucket_key": "b1", "doc_type": "bank_statement", "status": "filed"},
        {"bucket_key": "r1", "doc_type": "receipt", "status": "needs_review"},
        {"bucket_key": "i1", "doc_type": "invoice", "status": "posted"},
    ]
    expected = ["bank_statement", {"doc_type": "payroll", "label": "State de plata"}]
    state = document_state(docs, expected)
    by = {e["doc_type"]: e for e in state["expected"]}
    assert by["bank_statement"] == {"doc_type": "bank_statement", "label": "bank_statement",
                                    "count": 1, "keys": ["b1"]}  # fmt: skip
    assert state["missing"] == [{"doc_type": "payroll", "label": "State de plata"}]
    assert state["needs_review"] == ["r1"]
    assert document_state(docs, None)["missing"] == []
    with pytest.raises(ValueError, match="expected_documents"):
        document_state(docs, [{"label": "no type"}])


def test_an_empty_period_means_last_month() -> None:
    from langclaw.accounting.period import resolve_period

    assert resolve_period("", today=date(2026, 1, 5)) == "2025-12"
    assert resolve_period("last", today=date(2026, 9, 27)) == "2026-08"
    assert resolve_period(" 2026-03 ", today=date(2026, 9, 27)) == "2026-03"
    with pytest.raises(ValueError, match="YYYY-MM"):
        resolve_period("march", today=date(2026, 9, 27))


def test_vat_settlement_clears_4426_and_4427_into_4423_or_4424() -> None:
    from langclaw.accounting.period import vat_settlement

    payable = vat_settlement(deductible=D("235.54"), collected=D("462.00"))
    assert [(x["account"], x["debit"], x["credit"]) for x in payable["lines"]] == [
        ("4427", "462.00", "0"), ("4426", "0", "235.54"), ("4423", "0", "226.46")]  # fmt: skip
    refund = vat_settlement(deductible=D("500.00"), collected=D("100.00"))
    assert [(x["account"], x["debit"], x["credit"]) for x in refund["lines"]] == [
        ("4427", "100.00", "0"), ("4424", "400.00", "0"), ("4426", "0", "500.00")]  # fmt: skip
    only_in = vat_settlement(deductible=D("50.00"), collected=D("0"))
    assert [x["account"] for x in only_in["lines"]] == ["4424", "4426"]
    assert vat_settlement(deductible=D("0"), collected=D("0")) is None


def test_settlement_is_due_monthly_or_at_quarter_end_for_vat_payers() -> None:
    from langclaw.accounting.period import settles_vat

    assert settles_vat("2026-08", {"vat_payer": True})
    assert not settles_vat("2026-08", {"vat_payer": True, "vat_period": "quarterly"})
    assert settles_vat("2026-09", {"vat_payer": True, "vat_period": "quarterly"})
    assert not settles_vat("2026-09", {"vat_payer": False})
    assert settles_vat("2026-09", {"vat_payer": True, "vat_on_collection": True})


def test_d394_groups_by_partner_direction_and_rate() -> None:
    from langclaw.accounting.period import d394_rows

    def inv(direction, cui, name, rate, taxable, vat, doc_type="invoice", category="S"):
        partner = "receiver" if direction == "out" else "sender"
        return {"doc_type": doc_type, partner: name, "fields": {
            "direction": direction, "customer_cui" if direction == "out" else "supplier_cui": cui,
            "vat_breakdown": [{"category": category, "rate": rate, "taxable": taxable,
                               "vat": vat}]}}  # fmt: skip

    rows = d394_rows(
        [
            inv("out", "RO1", "Alfa", "21.00", "100.00", "21.00"),
            inv("out", "RO1", "Alfa", "21.00", "50.00", "10.50"),
            inv("out", "RO1", "Alfa", "21.00", "20.00", "4.20", doc_type="credit_note"),
            inv("out", "RO1", "Alfa", "11.00", "10.00", "1.10"),
            inv("in", "RO2", "Furnizor", "21.00", "300.00", "63.00"),
            inv("in", "RO3", "Import", "21.00", "80.00", "0", category="AE"),
            inv("in", "", "Fara CUI", "21.00", "5.00", "1.05"),  # no tax ID: not reportable
        ]
    )
    by = {(r["direction"], r["cui"], r["rate"], r["type"]): r for r in rows}
    alfa = by[("out", "RO1", "21", "normal")]
    assert (alfa["invoices"], alfa["taxable"], alfa["vat"]) == (3, D("130.00"), D("27.30"))
    assert by[("out", "RO1", "11", "normal")]["vat"] == D("1.10")
    assert by[("in", "RO3", "21", "reverse_charge")]["vat"] == D("16.80")
    assert not any(r["cui"] == "" for r in rows)


def test_the_trial_balance_sheet_has_the_five_column_pairs() -> None:
    from decimal import Decimal as D

    from langclaw.accounting.period import trial_balance_sheet

    before_year = [
        {"account": "5311", "debit": 100, "credit": 0},
        {"account": "1012", "debit": 0, "credit": 100},
    ]
    earlier = [
        {"account": "5311", "debit": 50, "credit": 0},
        {"account": "707", "debit": 0, "credit": 50},
    ]
    month = [{"account": "5311", "debit": 0, "credit": 30},
             {"account": "6022", "debit": 30, "credit": 0}]  # fmt: skip
    sheet = trial_balance_sheet(before_year, earlier, month)
    cash = next(r for r in sheet["accounts"] if r["account"] == "5311")
    assert (cash["opening_debit"], cash["previous_debit"], cash["month_credit"]) == (
        D("100.00"), D("50.00"), D("30.00"))  # fmt: skip
    assert (cash["total_debit"], cash["total_credit"]) == (D("150.00"), D("30.00"))
    assert (cash["closing_debit"], cash["closing_credit"]) == (D("120.00"), D("0.00"))
    capital = next(r for r in sheet["accounts"] if r["account"] == "1012")
    assert capital["opening_credit"] == capital["closing_credit"] == D("100.00")
    assert [r["account"] for r in sheet["accounts"]] == ["1012", "5311", "6022", "707"]
    totals = sheet["totals"]
    assert totals["opening_debit"] == totals["opening_credit"] == D("100.00")
    assert totals["total_debit"] == totals["total_credit"] == D("180.00")
    assert totals["closing_debit"] == totals["closing_credit"] and sheet["balanced"]


def test_an_opening_entry_puts_each_balance_on_its_side() -> None:
    import pytest

    from langclaw.accounting.period import opening_entry

    entry = opening_entry({"5121": "1000", "1012": "-800", "401": "-200", "4111": 0})
    assert [(x["account"], x["debit"], x["credit"]) for x in entry["lines"]] == [
        ("1012", "0", "800.00"), ("401", "0", "200.00"), ("5121", "1000.00", "0")]  # fmt: skip
    with pytest.raises(ValueError, match="balance"):
        opening_entry({"5121": "1000", "1012": "-900"})
    with pytest.raises(ValueError, match="account"):
        opening_entry({"cash": "10", "1012": "-10"})
    with pytest.raises(ValueError, match="No balances"):
        opening_entry({"4111": "0"})


def test_balance_anomalies_flag_balances_on_the_wrong_side() -> None:
    from langclaw.accounting.period import balance_anomalies

    accounts = [
        {"account": "5311", "balance": "-10.00"},
        {"account": "5121.01", "balance": "-5.00"},
        {"account": "581", "balance": "3.00"},
        {"account": "401", "balance": "20.00"},
        {"account": "4111", "balance": "-7.00"},
        {"account": "2813", "balance": "4.00"},
        {"account": "4111", "balance": "0.00"},
        {"account": "6022", "balance": "100.00"},
        {"account": "1012", "balance": "-100.00"},
    ]
    found = {a["account"]: a["problem"] for a in balance_anomalies(accounts)}
    assert set(found) == {"5311", "5121.01", "581", "401", "4111", "2813"}
    assert "cash" in found["5311"] and "overdraft" in found["5121.01"]
    assert "transit" in found["581"] and "supplier" in found["401"]
    assert "customer" in found["4111"] and "depreciation" in found["2813"]
