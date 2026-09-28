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
    """``{"period", "report", "outlook", "bank", "results", "partners", "cash"}`` for
    *tenant* and *period* (partner balances as of the month's last day).

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
    return {"period": period, "report": report, "outlook": outlook, "bank": bank,
            "results": results, "partners": partners, "cash": cash}  # fmt: skip


def _month_end(period: str) -> str:
    from langclaw.accounting.period import parse_period

    try:
        return parse_period(period)[1].isoformat()
    except ValueError:
        return ""
