"""
Accounting tools — the steps of the ``accounting_proposal`` workflow.

- ``accounting_context``: everything a proposal should be grounded in, gathered
  by code (the invoice, the client's VAT regime, how this partner was booked
  before, the VAT rates valid on the invoice date).
- ``accounting_check``: the deterministic verdict on a proposal.
- ``journal_post``: posts an entry — only if the checks pass.
- ``accounting_defer``: marks an invoice for manual booking.
- ``accounting_queue``: starts the workflow for invoices waiting for an entry.
- ``accounting_period_report`` / ``accounting_period_close``: a month's trial
  balance, VAT summary (D300 draft figures) and blockers; closing locks it.
- ``accounting_outlook``: facts for forward-looking advice (deadlines, regime
  thresholds, VAT trend) — the ``monthly_advice`` template turns them into advice.
- ``bank_import`` / ``bank_movements`` / ``bank_confirm_match``: bank statements
  (MT940 / CAMT.053) from the client's bucket, matched to the invoices they pay.
- ``accounting_export``: exports posted invoices to accounting software (SAGA
  import zip in the client's bucket under ``exports/<target>/``).

All of them work inside the current client (tenant) only, like the document tools.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from langclaw.accounting.bank.match import match_payments
from langclaw.accounting.bank.parse import BankStatementError, parse_statement
from langclaw.accounting.bank.store import BankBook
from langclaw.accounting.checks import check_proposal
from langclaw.accounting.export import ExportUnavailable, make_exporter
from langclaw.accounting.journal import Journal, JournalError
from langclaw.accounting.outlook import deadlines, thresholds, trend
from langclaw.accounting.period import (
    blockers,
    document_state,
    parse_period,
    trial_balance,
    vat_summary,
)
from langclaw.accounting.vat import allowed_vat_rates
from langclaw.documents.bucket import BucketError
from langclaw.documents.store import DocumentStoreError
from langclaw.tenants import current_tenant

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from langclaw.bus.base import BaseMessageBus
    from langclaw.documents.tools import DocumentServices

_ERRORS = (
    BankStatementError,
    BucketError,
    DocumentStoreError,
    JournalError,
    ExportUnavailable,
    ValueError,
)
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

    async def accounting_export(
        target: str = "saga", date_from: str = "", date_to: str = "", again: bool = False
    ) -> dict:
        """Export this client's posted invoices for accounting software; returns a link.

        Exported invoices get status "exported" and aren't exported twice unless
        *again* is set.

        Args:
            target: "saga" (XML import zip) or "nextup" (not available yet).
            date_from: Only invoices issued on or after this date (YYYY-MM-DD).
            date_to: Only invoices issued on or before this date (YYYY-MM-DD).
            again: Also include invoices already exported.
        """
        tenant = current_tenant()
        own_cif = tenant.tax_id if tenant else ""
        if not own_cif:
            return {"error": "The client has no tax ID (CIF): set it on the client first."}
        try:
            exporter = make_exporter(target)
            svc = services.current()
            rows = [
                r
                for status in (("posted", "exported") if again else ("posted",))
                for doc_type in _INVOICE_TYPES
                for r in await svc.store.search(
                    doc_type=doc_type,
                    status=status,
                    date_from=date_from,
                    date_to=date_to,
                    limit=200,
                )
            ]
            batch = exporter.build(rows, own_cif=own_cif)  # an unavailable target fails here
            if not rows:
                return {"exported": [], "note": "Nothing to export: no posted invoices."}
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            key = f"exports/{exporter.name}/{stamp}-{batch.filename}"
            if batch.exported:
                await svc.bucket.put(key, batch.data, content_type=batch.content_type)
                for done in batch.exported:
                    await svc.store.save(
                        done, {"status": "exported", "fields": {f"export_{exporter.name}": key}}
                    )
                url = await svc.bucket.link(key, expires_s=86400)
        except _ERRORS as exc:
            return {"error": str(exc)}
        if not batch.exported:
            return {"exported": [], "skipped": batch.skipped}
        return {"key": key, "url": url, "exported": batch.exported, "skipped": batch.skipped}

    async def _period_report(period: str) -> tuple[DocumentServices, dict[str, Any]]:
        start, end = parse_period(period)
        svc = services.current()
        journal = Journal(svc.store)
        docs = [
            r
            for doc_type in _INVOICE_TYPES
            for r in await svc.store.search(
                doc_type=doc_type, date_from=start.isoformat(), date_to=end.isoformat(), limit=200
            )
        ]
        month = await svc.store.search(
            date_from=start.isoformat(), date_to=end.isoformat(), limit=200
        )
        booked = [d for d in docs if d.get("status") in ("posted", "exported")]
        closed = {p["period"]: p for p in await journal.closed_periods()}
        report = {
            "period": period,
            "closed": closed.get(period),
            "blockers": blockers(docs),
            "documents": document_state(month, _profile().get("expected_documents")),
            "trial_balance": trial_balance(await journal.lines_between(start, end)),
            "vat": vat_summary(booked),
            "invoices": len(docs),
        }
        if len(docs) >= 200:
            report["note"] = "Over 200 invoices in the month: the report covers the first 200."
        return svc, json.loads(json.dumps(report, default=str))

    async def accounting_period_report(period: str) -> dict:
        """A month's close report: trial balance, VAT summary (D300 draft), blockers,
        and which expected documents (client profile ``expected_documents``) are in.

        Args:
            period: The month, as YYYY-MM.
        """
        try:
            _, report = await _period_report(period)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return report

    async def accounting_period_close(period: str, closed_by: str = "") -> dict:
        """Close a month: refused while invoices lack an entry or expected documents
        are missing; afterwards nothing
        can be posted with a date in it. Saves the report in the client's bucket.

        Args:
            period: The month, as YYYY-MM.
            closed_by: Who closed it.
        """
        try:
            svc, report = await _period_report(period)
            if report["closed"]:
                return {"error": f"Period {period} is already closed.", "closed": report["closed"]}
            if report["blockers"]:
                return {
                    "error": f"{len(report['blockers'])} invoice(s) in {period} have no entry yet.",
                    "blockers": report["blockers"],
                }
            if missing := report["documents"]["missing"]:
                labels = ", ".join(m["label"] for m in missing)
                return {"error": f"Expected documents missing for {period}: {labels}.",
                        "missing": missing}  # fmt: skip
            if not report["trial_balance"]["balanced"]:
                return {"error": "The trial balance doesn't balance; check the journal."}
            key = f"reports/{period}/close.json"
            await svc.bucket.put(
                key, json.dumps(report, indent=2).encode(), content_type="application/json"
            )
            await Journal(svc.store).close_period(period, closed_by=closed_by)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"closed": period, "report_key": key, "vat": report["vat"]}

    async def _invoices(svc: DocumentServices, start: date, end: date) -> list[dict[str, Any]]:
        return [
            r
            for doc_type in _INVOICE_TYPES
            for r in await svc.store.search(
                doc_type=doc_type, date_from=start.isoformat(), date_to=end.isoformat(), limit=200
            )
        ]

    async def accounting_outlook(period: str, months: int = 6) -> dict:
        """Facts to advise a client on what's coming: returns due after the month,
        how close the year's revenue is to regime limits, and the VAT trend.

        Args:
            period: The month just finished, as YYYY-MM.
            months: How many months of VAT history to compare (2-12).
        """
        tenant = current_tenant()
        try:
            start, end = parse_period(period)
            svc = services.current()
            profile = _profile()
            history = []
            for back in range(max(2, min(months, 12)) - 1, -1, -1):
                y, m = divmod(start.year * 12 + start.month - 1 - back, 12)
                label = f"{y:04d}-{m + 1:02d}"
                booked = [
                    r
                    for r in await _invoices(svc, *parse_period(label))
                    if r.get("status") in ("posted", "exported")
                ]
                vat = vat_summary(booked)
                history.append((label, vat["payable"] - vat["refundable"]))
            year_docs = await _invoices(svc, date(start.year, 1, 1), end)
        except _ERRORS as exc:
            return {"error": str(exc)}
        revenue = Decimal(0)
        for r in year_docs:
            f = r.get("fields") or {}
            if f.get("direction") == "out":
                net = abs(Decimal(str(f.get("total_net") or 0)))
                revenue += -net if r.get("doc_type") == "credit_note" else net
        in_month = [d for d in year_docs if str(d.get("document_date") or "") >= str(start)]
        facts: dict[str, Any] = {
            "period": period,
            "client": {"name": tenant.name if tenant else "", "profile": profile},
            "deadlines": deadlines(period, profile),
            "thresholds": thresholds(revenue, year=start.year, profile=profile),
            "revenue_ytd": revenue,
            "vat_trend": trend(history),
            "unbooked_invoices": len(blockers(in_month)),
        }
        if len(year_docs) >= 200:
            facts["note"] = "Over 200 invoices this year: revenue covers the first 200 per type."
        return json.loads(json.dumps(facts, default=str))

    async def _open_invoices(svc: DocumentServices) -> list[dict[str, Any]]:
        return [
            r
            for status in ("filed", "posted", "exported")
            for r in await svc.store.search(doc_type="invoice", status=status, limit=200)
            if not (r.get("fields") or {}).get("paid_on")
        ]

    async def _mark_paid(svc: DocumentServices, bucket_key: str, tx: dict[str, Any]) -> None:
        paid = {"paid_on": str(tx["booked"]), "payment_ref": tx.get("reference", ""),
                "payment_tx": tx["key"]}  # fmt: skip
        await svc.store.save(bucket_key, {"fields": paid})

    async def bank_import(key: str) -> dict:
        """Import a bank statement (MT940 or CAMT.053) from the client's bucket and
        match its movements to the invoices they pay. Safe to run twice.

        Certain matches (amount plus invoice number, IBAN or partner name) mark the
        invoice paid; probable ones (amount only) are listed to confirm with
        bank_confirm_match.

        Args:
            key: The statement file's key in the bucket.
        """
        try:
            svc = services.current()
            data, _ = await svc.bucket.get(key)
            statement = parse_statement(data)
            problems = statement.check()
            await svc.store.save(
                key,
                {
                    "doc_type": "bank_statement",
                    "document_date": statement.date_to or None,
                    "amount": statement.closing,
                    "currency": statement.currency,
                    "summary": (
                        f"Bank statement {statement.iban} {statement.date_from} – "
                        f"{statement.date_to}: {statement.opening} → {statement.closing} "
                        f"{statement.currency}, {len(statement.transactions)} movements."
                    ),
                    "status": "needs_review" if problems else "filed",
                    "fields": {
                        "source": "bank", "format": statement.format, "iban": statement.iban,
                        "date_from": statement.date_from, "date_to": statement.date_to,
                        "opening": str(statement.opening), "closing": str(statement.closing),
                        "movements": len(statement.transactions), "problems": problems,
                    },
                },
            )  # fmt: skip
            book = BankBook(svc.store)
            new = await book.add(key, statement.iban, statement.transactions)
            fresh = [t for t in statement.transactions if t.key in new]
            matches = match_payments(fresh, await _open_invoices(svc))
            by_key = {t.key: t for t in fresh}
            for m in matches:
                await book.set_match(m["key"], m["bucket_key"], m["kind"], m["because"])
                if m["kind"] == "certain":
                    t = by_key[m["key"]]
                    await _mark_paid(
                        svc,
                        m["bucket_key"],
                        {"key": t.key, "booked": t.booked, "reference": t.reference},
                    )
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {
            "statement": key,
            "iban": statement.iban,
            "period": [statement.date_from, statement.date_to],
            "problems": problems,
            "imported": len(new),
            "already_imported": len(statement.transactions) - len(new),
            "paid": [m for m in matches if m["kind"] == "certain"],
            "to_confirm": [m for m in matches if m["kind"] == "probable"],
            "unmatched": len(fresh) - len(matches),
        }

    async def bank_movements(unmatched_only: bool = True, limit: int = 50) -> dict:
        """The client's imported bank movements, newest first.

        Args:
            unmatched_only: Only movements without a certain match to an invoice.
            limit: Maximum movements (1-500).
        """
        try:
            rows = await BankBook(services.current().store).list(
                unmatched_only=unmatched_only, limit=limit
            )
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"movements": rows}

    async def bank_confirm_match(movement_key: str, bucket_key: str) -> dict:
        """Confirm that a bank movement pays an invoice (marks the invoice paid).

        Args:
            movement_key: The movement's key (from bank_import or bank_movements).
            bucket_key: The invoice's key.
        """
        try:
            svc = services.current()
            book = BankBook(svc.store)
            tx = await book.get(movement_key)
            if tx is None:
                return {"error": f"No bank movement {movement_key!r}."}
            _, row = await _invoice(bucket_key)
            if Decimal(str(row.get("amount") or 0)).quantize(Decimal("0.01")) != abs(tx["amount"]):
                return {
                    "error": f"The movement is {tx['amount']}, the invoice {row.get('amount')}."
                }
            await book.set_match(movement_key, bucket_key, "certain", "confirmed")
            await _mark_paid(svc, bucket_key, tx)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"paid": bucket_key, "movement": movement_key}

    fns = [
        accounting_context,
        accounting_check,
        journal_post,
        accounting_defer,
        accounting_export,
        accounting_period_report,
        accounting_period_close,
        accounting_outlook,
        bank_import,
        bank_movements,
        bank_confirm_match,
    ]
    if bus is not None:
        fns.append(accounting_queue)
    return [StructuredTool.from_function(coroutine=fn, parse_docstring=True) for fn in fns]
