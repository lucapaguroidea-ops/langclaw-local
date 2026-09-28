"""
One client's month at a glance — what the console's Client overview shows.

Runs the same accounting tools the agent and workflows use (close report,
outlook, open bank movements) inside the client's scope, so the page can never
disagree with what the tools report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langclaw.tenants import tenant_scope

if TYPE_CHECKING:
    from langclaw.documents.tools import DocumentServices
    from langclaw.tenants import Tenant


async def accounting_overview(
    services: DocumentServices, tenant: Tenant | None, period: str
) -> dict[str, Any]:
    """``{"period", "report", "outlook", "bank", "results", "partners", "cash",
    "reports"}`` for *tenant* and *period* (partner balances as of the month's last day).

    Each part is the tool's own result, so a failing part carries its
    ``{"error": ...}`` without hiding the others.
    """
    from contextlib import nullcontext

    from langclaw.accounting.tools import build_accounting_tools

    tools = {t.name: t for t in build_accounting_tools(services)}
    with tenant_scope(tenant) if tenant is not None else nullcontext():
        report = await tools["accounting_period_report"].ainvoke({"period": period})
        outlook = await tools["accounting_outlook"].ainvoke({"period": period})
        bank = await tools["bank_movements"].ainvoke({"unmatched_only": True, "limit": 50})
        results = await tools["accounting_results"].ainvoke({"period": period})
        end = report.get("period") and _month_end(report["period"])
        partners = await tools["partner_balances"].ainvoke({"day": end or ""})
        cash = await tools["cash_book"].ainvoke({"period": report.get("period") or period})
        reports = await tools["accounting_reports"].ainvoke(
            {"period": report.get("period") or period}
        )
    return {"period": period, "report": report, "outlook": outlook, "bank": bank,
            "results": results, "partners": partners, "cash": cash,
            "reports": reports}  # fmt: skip


def _month_end(period: str) -> str:
    from langclaw.accounting.period import parse_period

    try:
        return parse_period(period)[1].isoformat()
    except ValueError:
        return ""


def firm_row(client: str, name: str, report: dict[str, Any], bank: dict[str, Any]) -> dict:
    """One client's line in the firm-wide view, from its month report and its
    unmatched bank movements: what blocks the close, and the VAT to pay."""
    if "error" in report:
        return {"client": client, "name": name, "period": "", "closed": False, "blockers": 0,
                "missing": [], "bank_agrees": False, "unmatched": 0, "vat_to_pay": "",
                "vat_to_recover": "", "anomalies": 0, "result_to_carry": None,
                "ready_to_close": False, "error": report["error"]}  # fmt: skip
    vat = report.get("vat") or {}
    missing = [m.get("label") or m.get("doc_type", "")
               for m in (report.get("documents") or {}).get("missing") or []]  # fmt: skip
    row = {
        "client": client, "name": name, "period": report.get("period", ""),
        "closed": bool(report.get("closed")), "blockers": len(report.get("blockers") or []),
        "missing": missing, "bank_agrees": bool((report.get("bank") or {}).get("agrees", True)),
        "unmatched": int(bank.get("total") or 0), "vat_to_pay": vat.get("to_pay", ""),
        "vat_to_recover": vat.get("to_recover", ""),
        "anomalies": len(report.get("anomalies") or []),
        "result_to_carry": report.get("result_to_carry"), "error": "",
    }  # fmt: skip
    row["ready_to_close"] = not (row["closed"] or row["blockers"] or missing
                                 or not row["bank_agrees"] or row["anomalies"])  # fmt: skip
    row["error"] = row.pop("error")  # last, after ready_to_close
    return row


async def firm_overview(
    services: DocumentServices, tenants: list[Tenant], period: str, *, parallel: int = 6
) -> dict[str, Any]:
    """``{"period", "clients": [firm_row, ...]}``: every client's month, from the
    same tools as :func:`accounting_overview` (the close report and unmatched bank
    movements only). Up to *parallel* clients are checked at once, each in its
    own scope, and the rows keep the order of *tenants*."""
    import asyncio

    from langclaw.accounting import tools as accounting_tools

    tools = {t.name: t for t in accounting_tools.build_accounting_tools(services)}
    gate = asyncio.Semaphore(max(1, parallel))

    async def one(tenant: Tenant) -> dict[str, Any]:
        async with gate:
            with tenant_scope(tenant):
                report = await tools["accounting_period_report"].ainvoke({"period": period})
                bank = await tools["bank_movements"].ainvoke({"unmatched_only": True, "limit": 1})
        return firm_row(tenant.id, tenant.name, report, bank)

    rows = list(await asyncio.gather(*(one(t) for t in tenants)))
    resolved = next((r["period"] for r in rows if r["period"]), period)
    return {"period": resolved, "clients": rows}
