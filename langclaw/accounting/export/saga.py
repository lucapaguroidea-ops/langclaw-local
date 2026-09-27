"""
SAGA C. — "Import facturi XML" files.

One ``<Facturi><Factura><Antet/><Detalii><Continut><Linie/>…`` file per invoice,
named ``F_<cif>_<numar>_<data>.xml`` (the supplier's CIF). SAGA decides the
journal from the CIFs: the client as ``ClientCIF`` → purchases (Intrări), as
``FurnizorCIF`` → sales (Ieșiri). The batch is a zip with ``intrari/`` and
``iesiri/`` folders; SAGA books the invoices itself — our journal entries aren't
part of the file.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree as ET

from langclaw.accounting.export import ExportBatch

_CENT = Decimal("0.01")


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except InvalidOperation:
        return Decimal(0)


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT))


def _ro_date(value: Any, sep: str = ".") -> str:
    """``2026-09-15`` → ``15.09.2026`` (SAGA's date format)."""
    if not value:
        return ""
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return d.strftime(f"%d{sep}%m{sep}%Y")


def _rate(value: Decimal) -> str:
    return format(value.normalize(), "f")


def saga_filename(row: dict[str, Any]) -> str:
    """``F_<supplier CIF digits>_<number>_<dd-mm-yyyy>.xml``."""
    f = row.get("fields") or {}
    cif = re.sub(r"\D", "", f.get("supplier_cui", ""))
    number = re.sub(r"[^A-Za-z0-9]+", "-", f.get("invoice_number", "")).strip("-")
    return f"F_{cif}_{number}_{_ro_date(row.get('document_date'), '-')}.xml"


def saga_invoice_xml(row: dict[str, Any], *, own_cif: str) -> bytes:
    """The SAGA import XML for one invoice row (as imported from e-Factura).

    Raises:
        ValueError: the row lacks what SAGA needs (number, date, CIFs, lines).
    """
    f = row.get("fields") or {}
    missing = [
        k for k in ("invoice_number", "supplier_cui", "customer_cui", "lines") if not f.get(k)
    ]
    if not row.get("document_date"):
        missing.append("document_date")
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    breakdown = f.get("vat_breakdown") or []
    rates = {_dec(v.get("rate")) for v in breakdown}
    reverse = any(str(v.get("category", "")).upper() == "AE" for v in breakdown)

    root = ET.Element("Facturi")
    invoice = ET.SubElement(root, "Factura")
    head = ET.SubElement(invoice, "Antet")
    for tag, value in (
        ("FurnizorNume", row.get("sender", "")),
        ("FurnizorCIF", f.get("supplier_cui", "")),
        ("FurnizorNrRegCom", f.get("supplier_reg_com", "")),
        ("FurnizorIBAN", f.get("supplier_iban", "")),
        ("ClientNume", row.get("receiver", "")),
        ("ClientCIF", f.get("customer_cui", "")),
        ("FacturaNumar", f.get("invoice_number", "")),
        ("FacturaData", _ro_date(row.get("document_date"))),
        ("FacturaScadenta", _ro_date(f.get("due_date"))),
        ("FacturaTaxareInversa", "Da" if reverse else "Nu"),
        ("FacturaTVAIncasare", "Da" if f.get("vat_on_collection") else "Nu"),
        ("FacturaMoneda", row.get("currency") or "RON"),
        ("FacturaCotaTVA", _rate(next(iter(rates))) if len(rates) == 1 else ""),
    ):
        ET.SubElement(head, tag).text = str(value or "")
    content = ET.SubElement(ET.SubElement(invoice, "Detalii"), "Continut")
    for number, line in enumerate(f["lines"], 1):
        qty, net = _dec(line.get("quantity")) or Decimal(1), _dec(line.get("net"))
        vat = net * _dec(line.get("vat_rate")) / 100
        item = ET.SubElement(content, "Linie")
        for tag, value in (
            ("LinieNrCrt", str(number)),
            ("Descriere", line.get("name", "")),
            ("UM", line.get("unit", "")),
            ("Cantitate", _money(qty)),
            ("Pret", _money(net / qty)),
            ("Valoare", _money(net)),
            ("TVA", _money(vat)),
        ):
            ET.SubElement(item, tag).text = str(value or "")
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class SagaExporter:
    name = "saga"

    def build(self, rows: list[dict[str, Any]], *, own_cif: str) -> ExportBatch:
        ours = re.sub(r"\D", "", own_cif)
        buf = io.BytesIO()
        batch = ExportBatch(b"", "saga.zip", "application/zip")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for row in rows:
                f = row.get("fields") or {}
                try:
                    data = saga_invoice_xml(row, own_cif=own_cif)
                except ValueError as exc:
                    batch.skipped.append({"bucket_key": row["bucket_key"], "reason": str(exc)})
                    continue
                sale = re.sub(r"\D", "", f.get("supplier_cui", "")) == ours
                archive.writestr(f"{'iesiri' if sale else 'intrari'}/{saga_filename(row)}", data)
                batch.exported.append(row["bucket_key"])
        batch.data = buf.getvalue()
        return batch
