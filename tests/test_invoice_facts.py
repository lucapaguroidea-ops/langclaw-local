"""Scanned / PDF invoices → the accounting fields e-Factura invoices carry."""

from __future__ import annotations

from langclaw.accounting.invoice_facts import invoice_fields

FACTS = {
    "invoice_number": "F-102", "supplier_name": "Enel SRL", "supplier_cui": "RO 111",
    "customer_name": "ACME", "customer_cui": "12345678", "issue_date": "2026-09-10",
    "due_date": "2026-10-10", "currency": "ron", "total_net": "1,000.00",
    "total_vat": "210", "total_gross": "1210",
    "vat_breakdown": [{"rate": "21%", "taxable": "1000", "vat": "210"}],
}  # fmt: skip


def test_a_purchase_that_adds_up_gets_the_efactura_fields() -> None:
    out = invoice_fields(FACTS, own_cif="RO12345678")
    f = out["fields"]
    assert out["problems"] == [] and out["doc_type"] == "invoice"
    assert f["direction"] == "in" and f["source"] == "scan"
    assert f["supplier_cui"] == "RO111" and f["customer_cui"] == "RO12345678"
    assert (f["total_net"], f["total_vat"]) == ("1000.00", "210.00")
    assert f["vat_breakdown"] == [{"category": "S", "rate": "21", "taxable": "1000.00",
                                   "vat": "210.00"}]  # fmt: skip
    assert out["amount"] == "1210.00" and out["currency"] == "RON"


def test_a_sale_and_a_credit_note() -> None:
    sale = invoice_fields({**FACTS, "supplier_cui": "RO12345678", "customer_cui": "RO999"},
                          own_cif="12345678")  # fmt: skip
    assert sale["fields"]["direction"] == "out"
    credit = invoice_fields({**FACTS, "kind": "credit_note"}, own_cif="RO12345678")
    assert credit["doc_type"] == "credit_note"


def test_what_doesnt_add_up_is_named() -> None:
    bad = invoice_fields({**FACTS, "total_vat": "200", "total_gross": "1300"},
                         own_cif="RO12345678")  # fmt: skip
    joined = " ".join(bad["problems"])
    assert "VAT lines add up to 210.00" in joined and "1300.00" in joined
    old_rate = invoice_fields({**FACTS, "vat_breakdown": [
        {"rate": "19", "taxable": "1000", "vat": "190"}], "total_vat": "190",
        "total_gross": "1190"}, own_cif="RO12345678")  # fmt: skip
    assert any("19%" in p for p in old_rate["problems"])  # 19% ended on 2025-07-31
    stranger = invoice_fields({**FACTS, "customer_cui": "RO555"}, own_cif="RO12345678")
    assert any("neither" in p for p in stranger["problems"])
    empty = invoice_fields({"supplier_name": "X"}, own_cif="RO1")
    assert any("invoice number" in p for p in empty["problems"])
    assert any("date" in p for p in empty["problems"])


def test_a_missing_breakdown_is_derived_from_a_single_rate() -> None:
    out = invoice_fields({**FACTS, "vat_breakdown": []}, own_cif="RO12345678")
    assert out["fields"]["vat_breakdown"][0]["rate"] == "21" and out["problems"] == []
