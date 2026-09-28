"""Journal entries for bank movements: payments against invoices, bank fees."""

from __future__ import annotations

from langclaw.accounting.bank.booking import (
    bank_account,
    fee_entry,
    is_bank_fee,
    payment_entry,
)


def test_a_customer_payment_credits_the_customer_account_used_on_the_invoice() -> None:
    invoice = {"fields": {"direction": "out", "invoice_number": "FC-7"}}
    entry = [{"account": "4111", "debit": 1210.0, "credit": 0},
             {"account": "704", "debit": 0, "credit": 1000.0},
             {"account": "4427", "debit": 0, "credit": 210.0}]  # fmt: skip
    lines = payment_entry(invoice, "1210.00", invoice_lines=entry, bank="5121")["lines"]
    assert lines == [
        {"account": "5121", "debit": "1210.00", "credit": "0", "explanation": "Încasare FC-7"},
        {"account": "4111", "debit": "0", "credit": "1210.00", "explanation": "Încasare FC-7"},
    ]


def test_a_supplier_payment_debits_the_supplier_account_or_the_default() -> None:
    invoice = {"fields": {"direction": "in", "invoice_number": "17"}}
    booked = [{"account": "628", "debit": 100, "credit": 0},
              {"account": "404", "debit": 0, "credit": 100}]  # fmt: skip
    lines = payment_entry(invoice, "40.00", invoice_lines=booked, bank="5121")["lines"]
    assert [(x["account"], x["debit"], x["credit"]) for x in lines] == [
        ("404", "40.00", "0"), ("5121", "0", "40.00")]  # fmt: skip
    default = payment_entry(invoice, "40.00", invoice_lines=None, bank="5121")["lines"]
    assert default[0]["account"] == "401"


def test_bank_fees_and_accounts() -> None:
    assert is_bank_fee("Comision administrare cont") and is_bank_fee("TAXA BANCARA")
    assert not is_bank_fee("Plata Furnizor SRL")
    assert [(x["account"], x["debit"]) for x in fee_entry("12.50", bank="5121")["lines"]] == [
        ("627", "12.50"), ("5121", "0")]  # fmt: skip
    profile = {"bank_accounts": {"RO49AAAA1B31007593840000": "5121.01"}}
    assert bank_account("RO49AAAA1B31007593840000", "RON", profile) == "5121.01"
    assert bank_account("RO00OTHER", "RON", profile) == "5121"
    assert bank_account("RO00OTHER", "EUR", {}) == "5124"


def test_cash_deposits_and_withdrawals_go_through_581() -> None:
    from langclaw.accounting.bank.booking import cash_transfer, cash_transfer_entry

    assert cash_transfer("Depunere numerar casierie") == "deposit"
    assert cash_transfer("RETRAGERE NUMERAR ATM") == "withdrawal"
    assert cash_transfer("Plata Furnizor") is None
    dep = cash_transfer_entry("deposit", "500.00", bank="5121")["lines"]
    assert [(x["account"], x["debit"], x["credit"]) for x in dep] == [
        ("581", "500.00", "0"), ("5311", "0", "500.00"),
        ("5121", "500.00", "0"), ("581", "0", "500.00")]  # fmt: skip
    wd = cash_transfer_entry("withdrawal", "200.00", bank="5121")["lines"]
    assert [(x["account"], x["debit"], x["credit"]) for x in wd] == [
        ("581", "200.00", "0"), ("5121", "0", "200.00"),
        ("5311", "200.00", "0"), ("581", "0", "200.00")]  # fmt: skip


def test_vat_on_collection_moves_the_paid_share_out_of_4428() -> None:
    sale = {"amount": "1210.00", "fields": {"direction": "out", "invoice_number": "F1",
                                            "total_vat": "210.00"}}  # fmt: skip
    lines = payment_entry(sale, "605.00", invoice_lines=None, bank="5121",
                          vat_on_collection=True)["lines"]  # fmt: skip
    assert [(x["account"], x["debit"], x["credit"]) for x in lines[2:]] == [
        ("4428", "105.00", "0"), ("4427", "0", "105.00")]  # fmt: skip
    bill = {"amount": "121.00", "fields": {"direction": "in", "total_vat": "21.00"}}
    lines = payment_entry(bill, "121.00", invoice_lines=None, bank="5311",
                          vat_on_collection=True)["lines"]  # fmt: skip
    assert [(x["account"], x["debit"], x["credit"]) for x in lines[2:]] == [
        ("4426", "21.00", "0"), ("4428", "0", "21.00")]  # fmt: skip
    plain = payment_entry(bill, "121.00", invoice_lines=None, bank="5121")["lines"]
    assert len(plain) == 2
