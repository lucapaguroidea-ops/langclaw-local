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
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from langclaw.accounting.assets import FixedAssets, depreciation_entry, monthly_depreciation
from langclaw.accounting.bank.booking import bank_account, fee_entry, is_bank_fee, payment_entry
from langclaw.accounting.bank.match import match_payments, outstanding
from langclaw.accounting.bank.parse import BankStatementError, parse_statement
from langclaw.accounting.bank.store import BankBook
from langclaw.accounting.checks import check_proposal
from langclaw.accounting.export import ExportUnavailable, make_exporter
from langclaw.accounting.journal import Journal, JournalError
from langclaw.accounting.outlook import (
    cash_position,
    deadlines,
    overdue_receivables,
    thresholds,
    trend,
)
from langclaw.accounting.outlook import payables_due as plan_payables
from langclaw.accounting.period import (
    blockers,
    document_state,
    parse_period,
    resolve_period,
    settles_vat,
    trial_balance,
    vat_settlement,
    vat_summary,
)
from langclaw.accounting.results import profit_and_loss, tax_estimate
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
    mailer: Callable[[str, str, str], Awaitable[dict[str, Any]]] | None = None,
) -> list[BaseTool]:
    """The accounting tools over *services* (``accounting_queue`` needs *bus*).

    *mailer* ``(to, subject, body) -> {"draft_id"...}`` makes ``reminders_file``
    also create an email draft per reminder (the gateway passes Gmail's
    ``draft_email`` when Gmail has write access).
    """
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
        period = resolve_period(period)
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
        settlement = None
        if settles_vat(period, _profile()):
            settlement = vat_settlement(
                deductible=await journal.balance_until(end, "4426"),
                collected=-await journal.balance_until(end, "4427"),
            )
        report = {
            "period": period,
            "closed": closed.get(period),
            "blockers": blockers(docs),
            "documents": document_state(month, _profile().get("expected_documents")),
            "trial_balance": trial_balance(await journal.lines_between(start, end)),
            "vat": vat_summary(booked),
            "vat_settlement": settlement,
            "depreciation": depreciation_entry(await FixedAssets(svc.store).list(), period),
            "invoices": len(docs),
        }
        if len(docs) >= 200:
            report["note"] = "Over 200 invoices in the month: the report covers the first 200."
        return svc, json.loads(json.dumps(report, default=str))

    async def accounting_period_report(period: str = "") -> dict:
        """A month's close report: trial balance, VAT summary (D300 draft), blockers,
        and which expected documents (client profile ``expected_documents``) are in.

        Args:
            period: The month, as YYYY-MM (empty: last month).
        """
        try:
            _, report = await _period_report(period)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return report

    async def accounting_period_close(period: str = "", closed_by: str = "") -> dict:
        """Close a month: refused while invoices lack an entry or expected documents
        are missing. Posts the month's depreciation (6811 / 28xx) and, for VAT
        payers, the VAT settlement (4426/4427 → 4423 or 4424); saves the report in
        the client's bucket; then locks the month so nothing can be posted with a
        date in it.

        Args:
            period: The month, as YYYY-MM (empty: last month).
            closed_by: Who closed it.
        """
        try:
            svc, report = await _period_report(period)
            period = report["period"]
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
            depreciation = report["depreciation"]
            if depreciation:
                _, end = parse_period(period)
                doc = {"bucket_key": f"close/{period}/depreciation",
                       "document_date": end.isoformat(), "fields": {"direction": "in"}}  # fmt: skip
                try:
                    await Journal(svc.store).post(
                        doc, depreciation, approved_by=closed_by or "close"
                    )
                except JournalError as exc:
                    if "already posted" not in str(exc):
                        raise
            settled = report["vat_settlement"]
            if settled:
                _, end = parse_period(period)
                doc = {"bucket_key": f"close/{period}/vat-settlement",
                       "document_date": end.isoformat(), "fields": {"direction": "in"}}  # fmt: skip
                try:
                    await Journal(svc.store).post(doc, settled, approved_by=closed_by or "close")
                except JournalError as exc:
                    if "already posted" not in str(exc):
                        raise
                _, report = await _period_report(period)  # with the settlement booked
            key = f"reports/{period}/close.json"
            await svc.bucket.put(
                key, json.dumps(report, indent=2).encode(), content_type="application/json"
            )
            await Journal(svc.store).close_period(period, closed_by=closed_by)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"closed": period, "report_key": key, "vat": report["vat"],
                "vat_settlement": settled, "depreciation": depreciation}  # fmt: skip

    async def _invoices(svc: DocumentServices, start: date, end: date) -> list[dict[str, Any]]:
        return [
            r
            for doc_type in _INVOICE_TYPES
            for r in await svc.store.search(
                doc_type=doc_type, date_from=start.isoformat(), date_to=end.isoformat(), limit=200
            )
        ]

    async def accounting_outlook(period: str = "", months: int = 6) -> dict:
        """Facts to advise a client on what's coming: returns due after the month,
        how close the year's revenue is to regime limits, the VAT trend, cash
        (receivables / payables aging, bank balance, the next 30 days), and the
        year's result with an income-tax estimate.

        Args:
            period: The month just finished, as YYYY-MM (empty: last month).
            months: How many months of VAT history to compare (2-12).
        """
        tenant = current_tenant()
        try:
            period = resolve_period(period)
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
            open_docs = await _invoices(svc, date(1900, 1, 1), end)
            statements = await svc.store.search(
                doc_type="bank_statement", date_to=end.isoformat(), limit=50
            )
            results = await _results(svc, period)
        except _ERRORS as exc:
            return {"error": str(exc)}
        revenue = Decimal(0)
        for r in year_docs:
            f = r.get("fields") or {}
            if f.get("direction") == "out":
                net = abs(Decimal(str(f.get("total_net") or 0)))
                revenue += -net if r.get("doc_type") == "credit_note" else net
        latest: dict[str, Decimal] = {}  # newest closing balance per account
        for st in statements:  # newest first
            f = st.get("fields") or {}
            if f.get("iban") and f["iban"] not in latest and f.get("closing") is not None:
                latest[f["iban"]] = Decimal(str(f["closing"]))
        bank = sum(latest.values(), Decimal(0)) if latest else None
        in_month = [d for d in year_docs if str(d.get("document_date") or "") >= str(start)]
        facts: dict[str, Any] = {
            "period": period,
            "client": {"name": tenant.name if tenant else "", "profile": profile},
            "deadlines": deadlines(period, profile),
            "thresholds": thresholds(revenue, year=start.year, profile=profile),
            "revenue_ytd": revenue,
            "vat_trend": trend(history),
            "unbooked_invoices": len(blockers(in_month)),
            "cash": cash_position(open_docs, on=end, bank_balance=bank),
            "results_ytd": {
                "year_to_date": results["year_to_date"],
                "tax_estimate": results["tax_estimate"],
            },
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

    async def _apply_payment(
        svc: DocumentServices, bucket_key: str, amount: Decimal, tx: dict[str, Any]
    ) -> None:
        """Add *amount* from movement *tx* to the invoice's payments; ``paid_on`` is
        set once nothing is left to pay."""
        row = await svc.store.get(bucket_key) or {}
        f = row.get("fields") or {}
        payments = [p for p in f.get("payments") or [] if p.get("tx") != tx["key"]]
        payments.append({"tx": tx["key"], "date": str(tx["booked"]), "amount": str(amount),
                         "reference": tx.get("reference", "")})  # fmt: skip
        paid = sum((Decimal(p["amount"]) for p in payments), Decimal(0))
        values: dict[str, Any] = {"payments": payments, "paid_amount": str(paid)}
        if outstanding({**row, "fields": {**f, "paid_amount": paid}}) <= 0:
            values.update(paid_on=str(tx["booked"]), payment_ref=tx.get("reference", ""),
                          payment_tx=tx["key"])  # fmt: skip
        await svc.store.save(bucket_key, {"fields": values})

    async def _book(svc: DocumentServices, doc: dict[str, Any], entry: dict[str, Any]) -> str:
        """Post a bank entry; ``""`` when posted (or already posted), else why not."""
        try:
            await Journal(svc.store).post(doc, entry, approved_by="bank")
        except JournalError as exc:
            return "" if "already posted" in str(exc) else str(exc)
        return ""

    async def _book_payment(
        svc: DocumentServices, bucket_key: str, amount: Decimal, tx: dict[str, Any]
    ) -> str:
        """The journal entry for a payment applied to an invoice ("" or why not)."""
        row = await svc.store.get(bucket_key) or {}
        invoice_entry = await Journal(svc.store).get(bucket_key)
        bank = bank_account(tx.get("account_iban", ""), tx.get("currency", "RON"), _profile())
        doc = {**row, "bucket_key": f"bank/{tx['key']}/{bucket_key}",
               "document_date": str(tx["booked"])}  # fmt: skip
        entry = payment_entry(
            row, str(amount), invoice_lines=(invoice_entry or {}).get("lines"), bank=bank
        )
        return await _book(svc, doc, entry)

    async def bank_import(key: str) -> dict:
        """Import a bank statement (MT940 or CAMT.053) from the client's bucket, match
        its movements to the invoices they pay and book them (payments: 5121 against
        the invoice's partner account; bank fees: 627). Safe to run twice.

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
            booked, not_booked, fees = 0, [], []
            for m in matches:
                keys = ",".join(a["bucket_key"] for a in m["allocations"])
                await book.set_match(m["key"], keys, m["kind"], m["because"])
                if m["kind"] in ("certain", "partial"):
                    t = by_key[m["key"]]
                    tx = {"key": t.key, "booked": t.booked, "reference": t.reference,
                          "account_iban": statement.iban, "currency": t.currency}  # fmt: skip
                    for a in m["allocations"]:
                        await _apply_payment(svc, a["bucket_key"], Decimal(a["amount"]), tx)
                        why = await _book_payment(svc, a["bucket_key"], Decimal(a["amount"]), tx)
                        if why:
                            not_booked.append({"movement": t.key, "reason": why})
                        else:
                            booked += 1
            matched = {m["key"] for m in matches}
            for t in fresh:
                if t.key in matched or t.amount >= 0:
                    continue
                if not is_bank_fee(f"{t.counterparty} {t.description}"):
                    continue
                bank = bank_account(statement.iban, t.currency, _profile())
                doc = {"bucket_key": f"bank/{t.key}/fee", "document_date": t.booked,
                       "fields": {"direction": "in"}, "sender": t.counterparty}  # fmt: skip
                why = await _book(svc, doc, fee_entry(str(abs(t.amount)), bank=bank))
                if why:
                    not_booked.append({"movement": t.key, "reason": why})
                    continue
                await book.set_match(t.key, "", "fee", "bank fee")
                fees.append({"key": t.key, "amount": str(t.amount), "description": t.description})
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
            "partial": [m for m in matches if m["kind"] == "partial"],
            "fees": fees,
            "booked_entries": booked,
            "not_booked": not_booked,
            "to_confirm": [m for m in matches if m["kind"] == "probable"],
            "unmatched": len(fresh) - len(matches) - len(fees),
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
        """Confirm that a bank movement pays an invoice, fully or in part (the
        invoice is marked paid once nothing is left to pay).

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
            amount, left = abs(tx["amount"]), outstanding(row)
            if amount > left:
                return {"error": f"The movement is {tx['amount']}, but only {left} is left "
                                 f"to pay on {bucket_key}."}  # fmt: skip
            kind = "certain" if amount == left else "partial"
            await book.set_match(movement_key, bucket_key, kind, "confirmed")
            await _apply_payment(svc, bucket_key, amount, tx)
            why = await _book_payment(svc, bucket_key, amount, tx)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"paid" if kind == "certain" else "partly_paid": bucket_key,
                "movement": movement_key, "left": str(left - amount),
                **({"not_booked": why} if why else {})}  # fmt: skip

    async def assets_add(
        name: str, account: str, value: float, in_service: str, life_months: int
    ) -> dict:
        """Register a fixed asset; it depreciates linearly from the month after
        *in_service*, posted when each month is closed.

        Args:
            name: What it is (e.g. "Laptop Dell").
            account: Its fixed-asset account (20x / 21x, e.g. 2131, 214, 205).
            value: Entry value, without VAT.
            in_service: Date put into service (YYYY-MM-DD).
            life_months: Useful life in months (from the catalogue of useful lives).
        """
        try:
            asset = await FixedAssets(services.current().store).add(
                name, account, Decimal(str(value)), in_service, int(life_months)
            )
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"asset": {"id": asset.id, "name": asset.name, "account": asset.account,
                          "value": str(asset.value), "in_service": asset.in_service,
                          "life_months": asset.life_months}}  # fmt: skip

    async def assets_list(period: str = "") -> dict:
        """The client's fixed assets with this month's depreciation.

        Args:
            period: The month, as YYYY-MM (empty: last month).
        """
        try:
            period = resolve_period(period)
            assets = await FixedAssets(services.current().store).list()
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {
            "period": period,
            "assets": [
                {"id": a.id, "name": a.name, "account": a.account, "value": str(a.value),
                 "in_service": a.in_service, "life_months": a.life_months,
                 "this_month": str(monthly_depreciation(a, period))}
                for a in assets
            ],
        }  # fmt: skip

    async def _results(svc: DocumentServices, period: str) -> dict[str, Any]:
        start, end = parse_period(period)
        journal = Journal(svc.store)
        month = profit_and_loss(await journal.lines_between(start, end))
        ytd = profit_and_loss(await journal.lines_between(date(start.year, 1, 1), end))
        return {
            "period": period,
            "month": month,
            "year_to_date": ytd,
            "tax_estimate": tax_estimate(ytd, _profile()),
        }

    async def accounting_results(period: str = "") -> dict:
        """Profit and loss from the journal for the month and the year to date, and
        an estimate of the income tax (micro-enterprise or profit tax, per the
        client's profile).

        Args:
            period: The month, as YYYY-MM (empty: last month).
        """
        try:
            result = await _results(services.current(), resolve_period(period))
        except _ERRORS as exc:
            return {"error": str(exc)}
        return json.loads(json.dumps(result, default=str))

    async def partner_statement(partner_cui: str, date_from: str = "", date_to: str = "") -> dict:
        """A partner's statement (fișa partenerului): opening balance, every movement
        on its partner accounts (401/404/408, 411/4111/418) with a running balance
        (debit − credit: positive means they owe the client), and the closing balance.

        Args:
            partner_cui: The partner's tax ID, as on its invoices (e.g. RO87654321).
            date_from: First day (YYYY-MM-DD); empty: from the beginning.
            date_to: Last day (YYYY-MM-DD); empty: up to today.
        """
        if not partner_cui.strip():
            return {"error": "Give the partner_cui (the partner's tax ID)."}
        try:
            start = date.fromisoformat(date_from) if date_from else None
            end = date.fromisoformat(date_to) if date_to else None
            lines = await Journal(services.current().store).partner_lines(partner_cui, end)
        except _ERRORS as exc:
            return {"error": str(exc)}
        cent = Decimal("0.01")
        opening = sum(
            (Decimal(x["debit"]) - Decimal(x["credit"]) for x in lines
             if start and x["entry_date"] < start),
            Decimal(0),
        )  # fmt: skip
        balance, movements = opening, []
        for x in lines:
            if start and x["entry_date"] < start:
                continue
            balance += Decimal(x["debit"]) - Decimal(x["credit"])
            movements.append(
                {"date": x["entry_date"].isoformat(), "document": x["bucket_key"],
                 "account": x["account"], "explanation": x["explanation"],
                 "debit": str(Decimal(x["debit"]).quantize(cent)),
                 "credit": str(Decimal(x["credit"]).quantize(cent)),
                 "balance": str(balance.quantize(cent))}
            )  # fmt: skip
        name = next((x["partner_name"] for x in lines if x["partner_name"]), "")
        return {
            "partner": {"cui": partner_cui, "name": name},
            "opening": str(opening.quantize(cent)),
            "movements": movements,
            "closing": str(balance.quantize(cent)),
        }

    async def partner_balances(day: str = "") -> dict:
        """Every partner with an open balance on *day*: what customers owe (41x) and
        what the client owes suppliers (40x) — the list for balance confirmations.

        Args:
            day: The date (YYYY-MM-DD); empty: today.
        """
        try:
            on = date.fromisoformat(day) if day else date.today()
            rows = await Journal(services.current().store).partner_balances(on)
        except _ERRORS as exc:
            return {"error": str(exc)}
        cent = Decimal("0.01")
        partners = [
            {"cui": r["partner_cui"], "name": r["name"],
             "receivable": str(Decimal(r["rec"]).quantize(cent)),
             "payable": str(Decimal(r["pay"]).quantize(cent))}
            for r in rows if r["rec"] or r["pay"]
        ]  # fmt: skip
        return {"day": on.isoformat(), "partners": partners}

    async def receivables_overdue(day: str = "", min_days: int = 7) -> dict:
        """Customers with unpaid sales invoices past due, with the invoices, days
        overdue and what's left to pay — what payment reminders are drafted from.

        Args:
            day: The date to measure against (YYYY-MM-DD); empty: today.
            min_days: Only invoices at least this many days past due.
        """
        try:
            on = date.fromisoformat(day) if day else date.today()
            rows = await _invoices(services.current(), date(1900, 1, 1), on)
        except _ERRORS as exc:
            return {"error": str(exc)}
        tenant = current_tenant()
        customers = overdue_receivables(rows, on=on, min_days=max(0, int(min_days)))
        return json.loads(json.dumps(
            {"day": on.isoformat(), "client": tenant.name if tenant else "",
             "customers": customers}, default=str))  # fmt: skip

    async def reminders_file(reminders: list[dict[str, Any]], day: str = "") -> dict:
        """File approved payment reminders: each is saved in the client's bucket as
        a ``payment_reminder`` document, and every invoice it cites gets the date
        added to its reminder history (so the next reminder can escalate). With
        Gmail connected, an email draft to the customer is created too.

        Args:
            reminders: [{"partner", "cui", "subject", "body", "invoices": [bucket_key, ...]}].
                Without "invoices", the customer's currently overdue invoices are used.
            day: The date sent (YYYY-MM-DD); empty: today.
        """
        try:
            on = date.fromisoformat(day) if day else date.today()
            svc = services.current()
            overdue = overdue_receivables(
                await _invoices(svc, date(1900, 1, 1), on), on=on, min_days=1
            )
            by_cui = {c["cui"]: [i["bucket_key"] for i in c["invoices"]] for c in overdue}
            emails = {c["cui"]: c.get("email", "") for c in overdue}
            filed = []
            for n, r in enumerate(reminders or [], 1):
                if isinstance(r, str):
                    r = json.loads(r)
                cui = str(r.get("cui") or "")
                keys = list(r.get("invoices") or by_cui.get(cui, []))
                key = f"reminders/{on.isoformat()}/{cui or 'partner'}-{n}.txt"
                text = f"{r.get('subject', '')}\n\n{r.get('body', '')}".strip()
                await svc.bucket.put(key, text.encode(), content_type="text/plain")
                await svc.store.save(
                    key,
                    {
                        "doc_type": "payment_reminder",
                        "document_date": on.isoformat(),
                        "receiver": r.get("partner", ""),
                        "summary": r.get("subject", ""),
                        "status": "filed",
                        "fields": {"customer_cui": cui, "invoices": keys},
                    },
                )
                for inv_key in keys:
                    row = await svc.store.get(inv_key) or {}
                    history = list((row.get("fields") or {}).get("reminders") or [])
                    await svc.store.save(
                        inv_key,
                        {
                            "fields": {
                                "reminders": [*history, on.isoformat()],
                                "reminded_on": on.isoformat(),
                            }
                        },
                    )
                item = {"document": key, "partner": r.get("partner", ""), "invoices": keys}
                to = str(r.get("email") or emails.get(cui, ""))
                if mailer is not None:
                    if not to:
                        item["draft"] = "no email address for this customer"
                    else:
                        sent = await mailer(to, str(r.get("subject", "")), str(r.get("body", "")))
                        item["draft"] = sent.get("error") or sent.get("draft_id", "")
                        item["to"] = to
                filed.append(item)
        except (*_ERRORS, json.JSONDecodeError) as exc:
            return {"error": str(exc)}
        return {"filed": filed}  # fmt: skip

    async def _payables(day: str, days: int) -> tuple[DocumentServices, date, list[dict]]:
        on = date.fromisoformat(day) if day else date.today()
        svc = services.current()
        rows = await _invoices(svc, date(1900, 1, 1), on.replace(year=on.year + 1))
        return svc, on, plan_payables(rows, on=on, days=max(0, int(days)))

    async def payables_due(day: str = "", days: int = 7) -> dict:
        """Supplier invoices to pay: unpaid, due within *days* (overdue included),
        grouped by supplier with IBAN, amount left and the invoice numbers.

        Args:
            day: The date to plan from (YYYY-MM-DD); empty: today.
            days: How many days ahead to include.
        """
        try:
            _, on, due = await _payables(day, days)
        except _ERRORS as exc:
            return {"error": str(exc)}
        total = sum((d["amount"] for d in due), Decimal(0))
        return json.loads(json.dumps({"day": on.isoformat(), "total": total, "suppliers": due},
                                     default=str))  # fmt: skip

    async def payables_batch(day: str = "", days: int = 7) -> dict:
        """Write a payment batch (CSV: beneficiary, tax ID, IBAN, amount, currency,
        payment details) for the supplier invoices due within *days*, into the
        client's bucket under payments/, and return a download link. Suppliers
        without an IBAN are left out and listed.

        Args:
            day: The date to plan from (YYYY-MM-DD); empty: today.
            days: How many days ahead to include.
        """
        import csv
        import io

        try:
            svc, on, due = await _payables(day, days)
            payable = [d for d in due if d["iban"]]
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=";")
            writer.writerow(["beneficiary", "tax_id", "iban", "amount", "currency", "details"])
            for d in payable:
                details = "Plata fact. " + ", ".join(n for n in d["numbers"] if n)
                writer.writerow([d["partner"], d["cui"], d["iban"], f"{d['amount']:.2f}",
                                 d["currency"], details[:140]])  # fmt: skip
            key = f"payments/{on.isoformat()}-batch.csv"
            if payable:
                await svc.bucket.put(key, buf.getvalue().encode("utf-8"), content_type="text/csv")
                total = sum((d["amount"] for d in payable), Decimal(0))
                cited = [k for d in payable for k in d["invoices"]]
                await svc.store.save(
                    key,
                    {
                        "doc_type": "payment_batch",
                        "document_date": on.isoformat(),
                        "amount": total,
                        "status": "filed",
                        "summary": f"{len(payable)} supplier payment(s), {total:.2f}",
                        "fields": {"invoices": cited},
                    },
                )
                url = await svc.bucket.link(key, expires_s=86400)
        except _ERRORS as exc:
            return {"error": str(exc)}
        missing = [{"partner": d["partner"], "amount": str(d["amount"])} for d in due
                   if not d["iban"]]  # fmt: skip
        if not payable:
            return {"payments": 0, "missing_iban": missing}
        return {"key": key, "url": url, "payments": len(payable), "total": f"{total:.2f}",
                "missing_iban": missing}  # fmt: skip

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
        assets_add,
        assets_list,
        accounting_results,
        partner_statement,
        partner_balances,
        receivables_overdue,
        reminders_file,
        payables_due,
        payables_batch,
    ]
    if bus is not None:
        fns.append(accounting_queue)
    return [StructuredTool.from_function(coroutine=fn, parse_docstring=True) for fn in fns]
