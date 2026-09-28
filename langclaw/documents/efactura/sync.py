"""
File e-Factura invoices for one client: SPV → bucket → documents table.

Each SPV message becomes ``efactura/<received|sent>/<id>.xml`` in the client's
bucket folder and one ``documents`` row keyed by that path, so a sync can run
any number of times (a scheduled job, a retry) and files each invoice once.
The XML is parsed deterministically; an invoice whose totals don't add up is
still filed, with status ``needs_review`` and the problems listed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from loguru import logger

from langclaw.documents.efactura.spv import SpvClient, SpvError, cif_digits, unzip_invoice
from langclaw.documents.efactura.ubl import UblError, UblInvoice, parse_ubl

if TYPE_CHECKING:
    from langclaw.documents.tools import DocumentServices

#: Message kinds that carry an invoice.
INVOICE_KINDS = ("received", "sent")


def bucket_key(kind: str, message_id: str) -> str:
    return f"efactura/{kind}/{message_id}.xml"


async def sync_efactura(
    services: DocumentServices, spv: SpvClient, *, cif: str, days: int = 60
) -> dict[str, Any]:
    """File every new e-Factura invoice of *cif* into *services* (a client's view).

    Returns ``{"filed": [keys], "skipped": n_already_filed, "errors": [...]}``.
    One invoice failing doesn't stop the others.

    Raises:
        SpvError: the message list itself couldn't be fetched.
    """
    messages = [m for m in await spv.list_messages(cif, days) if m.kind in INVOICE_KINDS]
    keys = {m.id: bucket_key(m.kind, m.id) for m in messages}
    known = await services.store.known_keys(list(keys.values()))
    filed: list[str] = []
    errors: list[str] = []
    for msg in messages:
        key = keys[msg.id]
        if key in known:
            continue
        try:
            xml = unzip_invoice(await spv.download(msg.id))
            invoice = parse_ubl(xml)
            await services.bucket.put(key, xml, content_type="application/xml")
            saved = await services.store.save(key, invoice_record(invoice, msg, own_cif=cif))
        except (SpvError, UblError) as exc:
            errors.append(f"{msg.id}: {exc}")
            logger.warning(f"e-Factura {msg.id} for {cif_digits(cif)} not filed: {exc}")
            continue
        if services.embeddings is not None:
            await services.embed_record(saved)
        filed.append(key)
    return {"filed": filed, "skipped": len(known), "errors": errors}


def invoice_record(invoice: UblInvoice, msg: Any, *, own_cif: str) -> dict[str, Any]:
    """The ``documents`` row for a parsed invoice."""
    ours = cif_digits(own_cif)
    direction = "out" if cif_digits(invoice.supplier.cui) == ours else "in"
    problems = invoice.check()
    label = "Credit note" if invoice.kind == "credit_note" else "Invoice"
    return {
        "filename": f"{invoice.number or msg.id}.xml",
        "mime_type": "application/xml",
        "doc_type": invoice.kind,
        "sender": invoice.supplier.name,
        "receiver": invoice.customer.name,
        "document_date": invoice.issue_date or None,
        "amount": invoice.total_gross,
        "currency": invoice.currency,
        "summary": (
            f"{label} {invoice.number} from {invoice.supplier.name} to "
            f"{invoice.customer.name}: {invoice.total_net} + VAT {invoice.total_vat} = "
            f"{invoice.total_gross} {invoice.currency}."
        ),
        "status": "needs_review" if problems else "filed",
        "fields": {
            "source": "efactura",
            "direction": direction,
            "spv_id": msg.id,
            "spv_created": msg.created,
            "invoice_number": invoice.number,
            "due_date": invoice.due_date,
            "supplier_cui": invoice.supplier.cui,
            "supplier_reg_com": invoice.supplier.reg_com,
            "supplier_iban": invoice.supplier.iban,
            "customer_cui": invoice.customer.cui,
            "supplier_email": invoice.supplier.email,
            "customer_email": invoice.customer.email,
            "total_net": _s(invoice.total_net),
            "total_vat": _s(invoice.total_vat),
            "vat_breakdown": [
                {
                    "category": v.category,
                    "rate": _s(v.rate),
                    "taxable": _s(v.taxable),
                    "vat": _s(v.vat),
                }
                for v in invoice.vat_breakdown
            ],
            "lines": [
                {
                    "name": line.name,
                    "quantity": _s(line.quantity),
                    "unit": line.unit,
                    "net": _s(line.net),
                    "vat_rate": _s(line.vat_rate) if line.vat_rate is not None else None,
                }
                for line in invoice.lines
            ],
            "problems": problems,
        },
    }


def _s(value: Decimal) -> str:
    return str(value)


def make_spv_client(config: Any) -> SpvClient:
    """The SPV client for ``documents.efactura`` (``demo`` or ``anaf``)."""
    from langclaw.documents.efactura.spv import AnafSpvClient, DemoSpvClient

    if config.mode == "anaf":
        return AnafSpvClient(config.token, environment=config.environment)
    return DemoSpvClient(invoices=config.demo_invoices)
