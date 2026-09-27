"""
Accounting tools — the steps of the ``accounting_proposal`` workflow.

- ``accounting_context``: everything a proposal should be grounded in, gathered
  by code (the invoice, the client's VAT regime, how this partner was booked
  before, the VAT rates valid on the invoice date).
- ``accounting_check``: the deterministic verdict on a proposal.
- ``journal_post``: posts an entry — only if the checks pass.
- ``accounting_defer``: marks an invoice for manual booking.
- ``accounting_queue``: starts the workflow for invoices waiting for an entry.

All of them work inside the current client (tenant) only, like the document tools.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

from langclaw.accounting.checks import check_proposal
from langclaw.accounting.journal import Journal, JournalError
from langclaw.accounting.vat import allowed_vat_rates
from langclaw.documents.store import DocumentStoreError
from langclaw.tenants import current_tenant

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from langclaw.bus.base import BaseMessageBus
    from langclaw.documents.tools import DocumentServices

_ERRORS = (DocumentStoreError, JournalError, ValueError)
_INVOICE_TYPES = ("invoice", "credit_note")


def invoice_facts(row: dict[str, Any]) -> dict[str, Any]:
    """The facts :func:`check_proposal` judges a proposal against."""
    f = row.get("fields") or {}
    return {
        "direction": f.get("direction", "in"),
        "document_date": row.get("document_date"),
        "total_net": f.get("total_net"),
        "total_vat": f.get("total_vat"),
        "amount": row.get("amount"),
        "vat_breakdown": f.get("vat_breakdown") or [],
    }


def _profile() -> dict[str, Any]:
    tenant = current_tenant()
    return dict(tenant.profile) if tenant is not None else {}


def _proposal(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("The proposal must be an object with 'lines'.")
    return value


def build_accounting_tools(
    services: DocumentServices,
    *,
    bus: BaseMessageBus | None = None,
    report_to: dict[str, str] | None = None,
) -> list[BaseTool]:
    """The accounting tools over *services* (``accounting_queue`` needs *bus*)."""
    from langchain_core.tools import StructuredTool

    from langclaw.bus.base import InboundMessage

    cfg = services.config

    async def _invoice(bucket_key: str) -> tuple[DocumentServices, dict[str, Any]]:
        svc = services.current()
        row = await svc.store.get(bucket_key)
        if row is None:
            raise ValueError(f"No document {bucket_key!r}.")
        if row.get("doc_type") not in _INVOICE_TYPES:
            raise ValueError(
                f"{bucket_key!r} is a {row.get('doc_type') or 'document'}, not an invoice."
            )
        return svc, row

    async def accounting_context(bucket_key: str) -> dict:
        """Everything to ground a journal entry proposal for one invoice in.

        Args:
            bucket_key: The invoice's key (as filed, e.g. efactura/received/123.xml).
        """
        try:
            svc, row = await _invoice(bucket_key)
            journal = Journal(svc.store)
            f = row.get("fields") or {}
            incoming = f.get("direction", "in") != "out"
            partner = f.get("supplier_cui" if incoming else "customer_cui", "")
            history = await journal.supplier_history(partner)
            posted = bool(await journal.posted_keys([bucket_key]))
        except _ERRORS as exc:
            return {"error": str(exc)}
        tenant = current_tenant()
        issued = str(row.get("document_date") or "")[:10]
        rates = (
            sorted(allowed_vat_rates(date.fromisoformat(issued)), reverse=True) if issued else []
        )
        return {
            "invoice": {
                "bucket_key": bucket_key,
                "type": row.get("doc_type"),
                "direction": "in" if incoming else "out",
                "number": f.get("invoice_number", ""),
                "date": row.get("document_date"),
                "supplier": {"name": row.get("sender"), "cui": f.get("supplier_cui", "")},
                "customer": {"name": row.get("receiver"), "cui": f.get("customer_cui", "")},
                "total_net": f.get("total_net"),
                "total_vat": f.get("total_vat"),
                "total_gross": row.get("amount"),
                "currency": row.get("currency"),
                "vat_breakdown": f.get("vat_breakdown") or [],
                "lines": (f.get("lines") or [])[:30],
            },
            "client": {
                "name": tenant.name if tenant else "",
                "tax_id": tenant.tax_id if tenant else "",
                "profile": dict(tenant.profile) if tenant else {},
            },
            "partner_history": history,
            "valid_vat_rates": [f"{r.normalize()}" for r in rates],
            "posted": posted,
        }

    async def accounting_check(bucket_key: str, proposal: dict[str, Any]) -> dict:
        """Check a proposed journal entry against the invoice and the rules.

        Args:
            bucket_key: The invoice's key.
            proposal: {"lines": [{"account", "debit", "credit", "explanation"}], ...}.
        """
        try:
            _, row = await _invoice(bucket_key)
            problems = check_proposal(_proposal(proposal), invoice_facts(row), profile=_profile())
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"ok": not problems, "problems": problems}

    async def journal_post(
        bucket_key: str, proposal: dict[str, Any], approved_by: str = ""
    ) -> dict:
        """Post a journal entry for an invoice — refused unless every check passes.

        Args:
            bucket_key: The invoice's key.
            proposal: {"lines": [{"account", "debit", "credit", "explanation"}], ...}.
            approved_by: Who approved it, when a person did.
        """
        try:
            svc, row = await _invoice(bucket_key)
            entry = _proposal(proposal)
            problems = check_proposal(entry, invoice_facts(row), profile=_profile())
            if problems:
                return {"error": "Not posted: " + " ".join(problems)}
            posted = await Journal(svc.store).post(row, entry, approved_by=approved_by)
            await svc.store.save(bucket_key, {"status": "posted"})
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"posted": posted}

    async def accounting_defer(bucket_key: str, reason: str = "") -> dict:
        """Leave an invoice for manual booking (status needs_manual_entry).

        Args:
            bucket_key: The invoice's key.
            reason: Why (shown to the accountant).
        """
        try:
            svc, _ = await _invoice(bucket_key)
            saved = await svc.store.save(
                bucket_key, {"status": "needs_manual_entry", "fields": {"accounting_note": reason}}
            )
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"deferred": saved["bucket_key"]}

    async def accounting_queue(limit: int = 20) -> dict:
        """Start the accounting workflow for this client's filed invoices without an entry.

        Args:
            limit: Maximum invoices to start (1-200).
        """
        tenant = current_tenant() if services.require_tenant else None
        target = (tenant.review_target() if tenant else None) or report_to or {}
        channel, chat = target.get("channel", ""), target.get("chat_id", "")
        if not (channel and chat):
            return {"error": "No chat to report to: set the client's review chat."}
        try:
            svc = services.current()
            waiting = [
                r
                for doc_type in _INVOICE_TYPES
                for r in await svc.store.search(doc_type=doc_type, status="filed", limit=500)
            ]
            done = await Journal(svc.store).posted_keys([r["bucket_key"] for r in waiting])
        except _ERRORS as exc:
            return {"error": str(exc)}
        todo = [r["bucket_key"] for r in waiting if r["bucket_key"] not in done]
        todo = todo[: max(1, min(limit, 200))]
        for key in todo:
            await bus.publish(
                InboundMessage(
                    channel=channel,
                    user_id=chat,
                    context_id=chat,
                    chat_id=chat,
                    content=f"run workflow {cfg.accounting.workflow}",
                    origin="workflow",
                    metadata={
                        "workflow_name": cfg.accounting.workflow,
                        "workflow_input": json.dumps({"key": key}),
                        "trigger": "accounting_queue",
                        **({"tenant": tenant.id} if tenant else {}),
                    },
                )
            )
        return {"started": todo, "workflow": cfg.accounting.workflow}

    fns = [accounting_context, accounting_check, journal_post, accounting_defer]
    if bus is not None:
        fns.append(accounting_queue)
    return [StructuredTool.from_function(coroutine=fn, parse_docstring=True) for fn in fns]
