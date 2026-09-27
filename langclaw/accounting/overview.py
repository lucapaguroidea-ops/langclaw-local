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
    """``{"period", "report", "outlook", "bank"}`` for *tenant* and *period*.

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
    return {"period": period, "report": report, "outlook": outlook, "bank": bank}
