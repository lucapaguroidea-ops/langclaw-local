"""Exports to accounting software: SAGA XML import files, NextUp placeholder."""

from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

import pytest

from langclaw.accounting.export import EXPORTERS, ExportUnavailable, make_exporter
from langclaw.accounting.export.saga import saga_filename, saga_invoice_xml

ROW = {
    "bucket_key": "efactura/received/1.xml",
    "doc_type": "invoice",
    "sender": "Furnizor & Co SRL",
    "receiver": "ACME SRL",
    "document_date": "2026-09-15",
    "amount": 326.70,
    "currency": "RON",
    "fields": {
        "direction": "in",
        "invoice_number": "FC/0042",
        "due_date": "2026-10-15",
        "supplier_cui": "RO87654321",
        "supplier_reg_com": "J40/1/2020",
        "supplier_iban": "RO49AAAA1B31007593840000",
        "customer_cui": "RO12345678",
        "total_net": "270.00",
        "total_vat": "56.70",
        "vat_breakdown": [{"category": "S", "rate": "21.00", "taxable": "270.00", "vat": "56.70"}],
        "lines": [
            {"name": "Hosting", "quantity": "2.00", "unit": "H87", "net": "200.00",
             "vat_rate": "21.00"},
            {"name": "Support", "quantity": "1.00", "unit": "HUR", "net": "70.00",
             "vat_rate": "21.00"},
        ],  # fmt: skip
    },
}


def test_a_purchase_becomes_a_saga_invoice_with_the_client_as_buyer() -> None:
    root = ET.fromstring(saga_invoice_xml(ROW, own_cif="RO12345678"))
    assert root.tag == "Facturi"
    head = root.find("Factura/Antet")
    assert head.findtext("FurnizorNume") == "Furnizor & Co SRL"
    assert head.findtext("FurnizorCIF") == "RO87654321"
    assert head.findtext("FurnizorIBAN") == "RO49AAAA1B31007593840000"
    assert head.findtext("ClientCIF") == "RO12345678"
    assert head.findtext("FacturaNumar") == "FC/0042"
    assert head.findtext("FacturaData") == "15.09.2026"
    assert head.findtext("FacturaScadenta") == "15.10.2026"
    assert head.findtext("FacturaMoneda") == "RON"
    assert head.findtext("FacturaTaxareInversa") == "Nu"
    lines = root.findall("Factura/Detalii/Continut/Linie")
    assert [x.findtext("LinieNrCrt") for x in lines] == ["1", "2"]
    first = lines[0]
    assert first.findtext("Descriere") == "Hosting"
    assert first.findtext("Cantitate") == "2.00"
    assert first.findtext("Pret") == "100.00"
    assert first.findtext("Valoare") == "200.00"
    assert first.findtext("TVA") == "42.00"
    assert head.findtext("FacturaCotaTVA") == "21"


def test_reverse_charge_and_filename() -> None:
    row = {**ROW, "fields": {**ROW["fields"], "vat_breakdown": [{"category": "AE", "rate": "0"}]}}
    head = ET.fromstring(saga_invoice_xml(row, own_cif="RO12345678")).find("Factura/Antet")
    assert head.findtext("FacturaTaxareInversa") == "Da"
    assert saga_filename(ROW) == "F_87654321_FC-0042_15-09-2026.xml"


async def test_the_saga_exporter_splits_purchases_and_sales_into_a_zip() -> None:
    sale = {**ROW, "bucket_key": "efactura/sent/2.xml",
            "fields": {**ROW["fields"], "direction": "out", "supplier_cui": "RO12345678",
                       "customer_cui": "RO87654321"}}  # fmt: skip
    broken = {**ROW, "bucket_key": "x.pdf", "fields": {"direction": "in"}}
    batch = make_exporter("saga").build([ROW, sale, broken], own_cif="RO12345678")
    names = zipfile.ZipFile(io.BytesIO(batch.data)).namelist()
    assert "intrari/F_87654321_FC-0042_15-09-2026.xml" in names
    assert "iesiri/F_12345678_FC-0042_15-09-2026.xml" in names
    assert batch.exported == ["efactura/received/1.xml", "efactura/sent/2.xml"]
    assert batch.skipped[0]["bucket_key"] == "x.pdf"


def test_nextup_is_listed_but_honestly_unavailable() -> None:
    assert set(EXPORTERS) == {"saga", "nextup"}
    with pytest.raises(ExportUnavailable, match="NextUp"):
        make_exporter("nextup").build([ROW], own_cif="RO12345678")
    with pytest.raises(ValueError, match="saga"):
        make_exporter("winmentor")
