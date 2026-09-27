"""
Dummy e-Factura invoices — realistic CIUS-RO UBL for development and demos.

Structured like the Ministry of Finance examples, with today's VAT rates
(21% standard, 11% reduced). Nothing here is a real invoice.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from xml.sax.saxutils import escape

_CENT = Decimal("0.01")


@dataclass(slots=True)
class Party:
    name: str
    cui: str
    reg_com: str = ""
    iban: str = ""
    street: str = "Str. Exemplu nr. 1"
    city: str = "SECTOR1"
    county: str = "RO-B"


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _party_xml(tag: str, p: Party) -> str:
    legal = f"<cbc:CompanyLegalForm>{escape(p.reg_com)}</cbc:CompanyLegalForm>" if p.reg_com else ""
    return f"""<cac:{tag}><cac:Party>
    <cac:PartyName><cbc:Name>{escape(p.name)}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cbc:StreetName>{escape(p.street)}</cbc:StreetName>
      <cbc:CityName>{escape(p.city)}</cbc:CityName><cbc:CountrySubentity>{p.county}</cbc:CountrySubentity>
      <cac:Country><cbc:IdentificationCode>RO</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    <cac:PartyTaxScheme><cbc:CompanyID>{escape(p.cui)}</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity>
      <cbc:RegistrationName>{escape(p.name)}</cbc:RegistrationName>{legal}</cac:PartyLegalEntity>
  </cac:Party></cac:{tag}>"""


def make_invoice(
    *,
    number: str,
    issue_date: str,
    supplier: Party,
    customer: Party,
    lines: list[tuple[str, float, float, float]],
    due_date: str = "",
    currency: str = "RON",
    credit_note: bool = False,
) -> bytes:
    """A CIUS-RO invoice (or credit note) as UTF-8 XML.

    Args:
        lines: ``(description, quantity, unit_price, vat_rate_percent)`` each.
    """
    root, line_tag, qty_tag, type_tag, type_code = (
        ("CreditNote", "CreditNoteLine", "CreditedQuantity", "CreditNoteTypeCode", "381")
        if credit_note
        else ("Invoice", "InvoiceLine", "InvoicedQuantity", "InvoiceTypeCode", "380")
    )
    ns = "CreditNote-2" if credit_note else "Invoice-2"
    by_rate: dict[Decimal, Decimal] = defaultdict(Decimal)
    line_xml = []
    for i, (name, qty, price, rate) in enumerate(lines, 1):
        q, p, r = Decimal(str(qty)), Decimal(str(price)), Decimal(str(rate))
        net = (q * p).quantize(_CENT, rounding=ROUND_HALF_UP)
        by_rate[r] += net
        line_xml.append(
            f"""<cac:{line_tag}><cbc:ID>{i}</cbc:ID>
    <cbc:{qty_tag} unitCode="H87">{q}</cbc:{qty_tag}>
    <cbc:LineExtensionAmount currencyID="{currency}">{_money(net)}</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>{escape(name)}</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>{r}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="{currency}">{_money(p)}</cbc:PriceAmount></cac:Price>
  </cac:{line_tag}>"""
        )
    subtotals, total_net, total_vat = [], Decimal(0), Decimal(0)
    for rate in sorted(by_rate):
        taxable = by_rate[rate]
        vat = (taxable * rate / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
        total_net += taxable
        total_vat += vat
        subtotals.append(
            f"""<cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="{currency}">{_money(taxable)}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="{currency}">{_money(vat)}</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>{_money(rate)}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>"""
        )
    gross = total_net + total_vat
    payment = (
        f"""<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode>
    <cac:PayeeFinancialAccount><cbc:ID>{supplier.iban}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"""
        if supplier.iban
        else ""
    )
    due = f"<cbc:DueDate>{due_date}</cbc:DueDate>" if due_date and not credit_note else ""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{ns}"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:efactura.mfinante.ro:CIUS-RO:1.0.1</cbc:CustomizationID>
  <cbc:ID>{escape(number)}</cbc:ID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>{due}
  <cbc:{type_tag}>{type_code}</cbc:{type_tag}>
  <cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>
  {_party_xml("AccountingSupplierParty", supplier)}
  {_party_xml("AccountingCustomerParty", customer)}
  {payment}
  <cac:TaxTotal><cbc:TaxAmount currencyID="{currency}">{_money(total_vat)}</cbc:TaxAmount>
    {"".join(subtotals)}
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="{currency}">{_money(total_net)}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="{currency}">{_money(total_net)}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{currency}">{_money(gross)}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="{currency}">{_money(gross)}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  {"".join(line_xml)}
</{root}>"""
    return xml.encode()
