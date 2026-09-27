"""
Parse UBL 2.1 invoices and credit notes (the CIUS-RO profile used by e-Factura).

Deterministic and dependency-free: the result is what the document says, with
amounts as :class:`~decimal.Decimal`. :meth:`UblInvoice.check` reports totals
that don't add up instead of silently trusting them.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
_ROOTS = {
    "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice": "invoice",
    "{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote": "credit_note",
}
_CENT = Decimal("0.01")
_MAX_BYTES = 20_000_000


class UblError(ValueError):
    """The bytes aren't a readable UBL invoice / credit note."""


@dataclass(slots=True)
class UblParty:
    name: str = ""
    cui: str = ""
    """Fiscal code as written (``RO12345678`` for VAT payers)."""
    reg_com: str = ""
    """Trade register number (``J40/1234/2020``), when present."""
    iban: str = ""
    address: str = ""


@dataclass(slots=True)
class VatLine:
    category: str
    rate: Decimal
    taxable: Decimal
    vat: Decimal


@dataclass(slots=True)
class InvoiceLine:
    id: str
    name: str
    quantity: Decimal
    unit: str
    net: Decimal
    vat_rate: Decimal | None
    price: Decimal | None = None


@dataclass(slots=True)
class UblInvoice:
    kind: str  # "invoice" | "credit_note"
    number: str
    issue_date: str
    due_date: str
    currency: str
    type_code: str
    supplier: UblParty
    customer: UblParty
    total_net: Decimal
    total_vat: Decimal
    total_gross: Decimal
    payable: Decimal
    vat_breakdown: list[VatLine] = field(default_factory=list)
    lines: list[InvoiceLine] = field(default_factory=list)
    note: str = ""

    def check(self) -> list[str]:
        """Arithmetic problems in the document (empty when it adds up)."""
        problems: list[str] = []
        expected = self.total_net + self.total_vat
        if abs(expected - self.total_gross) > _CENT:
            problems.append(
                f"Total with VAT {self.total_gross} ≠ net {self.total_net} + "
                f"VAT {self.total_vat} = {expected}."
            )
        if self.vat_breakdown:
            vat_sum = sum((v.vat for v in self.vat_breakdown), Decimal(0))
            if abs(vat_sum - self.total_vat) > _CENT:
                problems.append(f"VAT lines add up to {vat_sum}, the total says {self.total_vat}.")
            taxable = sum((v.taxable for v in self.vat_breakdown), Decimal(0))
            if abs(taxable - self.total_net) > _CENT:
                problems.append(f"Taxable amounts add up to {taxable}, net is {self.total_net}.")
        return problems


def parse_ubl(data: bytes) -> UblInvoice:
    """Parse a UBL Invoice or CreditNote.

    Raises:
        UblError: not XML, or not a UBL invoice / credit note.
    """
    if len(data) > _MAX_BYTES:
        raise UblError("The file is too large to be an invoice.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise UblError(f"The file is not valid XML: {exc}") from None
    kind = _ROOTS.get(root.tag)
    if kind is None:
        raise UblError(f"The file is not a UBL Invoice or CreditNote (root {root.tag!r}).")

    currency = _text(root, "cbc:DocumentCurrencyCode") or "RON"
    type_code = _text(root, "cbc:InvoiceTypeCode") or _text(root, "cbc:CreditNoteTypeCode")
    supplier = _party(root.find("cac:AccountingSupplierParty/cac:Party", _NS))
    supplier.iban = _text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID")
    customer = _party(root.find("cac:AccountingCustomerParty/cac:Party", _NS))

    totals = root.find("cac:LegalMonetaryTotal", _NS)
    net = _amount(totals, "cbc:TaxExclusiveAmount")
    gross = _amount(totals, "cbc:TaxInclusiveAmount")
    payable = _amount(totals, "cbc:PayableAmount") if totals is not None else gross

    total_vat = Decimal(0)
    breakdown: list[VatLine] = []
    for tax_total in root.findall("cac:TaxTotal", _NS):
        amount = tax_total.find("cbc:TaxAmount", _NS)
        if amount is None or amount.get("currencyID", currency) != currency:
            continue  # a second TaxTotal in the accounting currency
        total_vat = _dec(amount.text)
        for sub in tax_total.findall("cac:TaxSubtotal", _NS):
            breakdown.append(
                VatLine(
                    category=_text(sub, "cac:TaxCategory/cbc:ID"),
                    rate=_dec(_text(sub, "cac:TaxCategory/cbc:Percent") or "0"),
                    taxable=_amount(sub, "cbc:TaxableAmount"),
                    vat=_amount(sub, "cbc:TaxAmount"),
                )
            )

    line_tag, qty_tag = (
        ("cac:InvoiceLine", "cbc:InvoicedQuantity")
        if kind == "invoice"
        else ("cac:CreditNoteLine", "cbc:CreditedQuantity")
    )
    lines = []
    for line in root.findall(line_tag, _NS):
        qty = line.find(qty_tag, _NS)
        rate = _text(line, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent")
        price = _text(line, "cac:Price/cbc:PriceAmount")
        lines.append(
            InvoiceLine(
                id=_text(line, "cbc:ID"),
                name=_text(line, "cac:Item/cbc:Name"),
                quantity=_dec(qty.text if qty is not None else "0"),
                unit=qty.get("unitCode", "") if qty is not None else "",
                net=_amount(line, "cbc:LineExtensionAmount"),
                vat_rate=_dec(rate) if rate else None,
                price=_dec(price) if price else None,
            )
        )

    return UblInvoice(
        kind=kind,
        number=_text(root, "cbc:ID"),
        issue_date=_text(root, "cbc:IssueDate"),
        due_date=_text(root, "cbc:DueDate") or _text(root, "cac:PaymentMeans/cbc:PaymentDueDate"),
        currency=currency,
        type_code=type_code,
        supplier=supplier,
        customer=customer,
        total_net=net,
        total_vat=total_vat,
        total_gross=gross,
        payable=payable,
        vat_breakdown=breakdown,
        lines=lines,
        note=_text(root, "cbc:Note"),
    )


def _party(node: ET.Element | None) -> UblParty:
    if node is None:
        return UblParty()
    legal_id = _text(node, "cac:PartyLegalEntity/cbc:CompanyID")
    legal_form = _text(node, "cac:PartyLegalEntity/cbc:CompanyLegalForm")
    cui = (
        _text(node, "cac:PartyTaxScheme/cbc:CompanyID")
        or (legal_id if legal_id and not _is_reg_com(legal_id) else "")
        or _text(node, "cac:PartyIdentification/cbc:ID")
    )
    reg_com = next((v for v in (legal_form, legal_id) if v and _is_reg_com(v)), "")
    address = ", ".join(
        v
        for v in (
            _text(node, "cac:PostalAddress/cbc:StreetName"),
            _text(node, "cac:PostalAddress/cbc:CityName"),
            _text(node, "cac:PostalAddress/cbc:CountrySubentity"),
        )
        if v
    )
    return UblParty(
        name=_text(node, "cac:PartyLegalEntity/cbc:RegistrationName")
        or _text(node, "cac:PartyName/cbc:Name"),
        cui=cui,
        reg_com=reg_com,
        address=address,
    )


def _is_reg_com(value: str) -> bool:
    return value[:1] in ("J", "F", "C") and "/" in value


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path, _NS)
    return (found.text or "").strip() if found is not None else ""


def _amount(node: ET.Element | None, path: str) -> Decimal:
    return _dec(_text(node, path) or "0")


def _dec(value: str | None) -> Decimal:
    try:
        return Decimal((value or "0").strip())
    except InvalidOperation as exc:
        raise UblError(f"Not a number: {value!r}") from exc
