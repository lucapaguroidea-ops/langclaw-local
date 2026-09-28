"""Ready-made RBAC roles for an accounting firm (use them in ``permissions.roles``).

- ``accountant``: every tool and workflow.
- ``client``: what a client's own staff may ask for in their chat — read-only
  reports about *their* company (the tools already run inside the chat's client
  scope). Nothing that posts, closes, reopens, reverses, files or emails.
  Files they send are still filed by the gateway's intake, which needs no tool.

``CLIENT_TOOLS`` is checked against the real tool names in the tests, so it
can't drift from the tools it names.
"""

from __future__ import annotations

from typing import Any

CLIENT_TOOLS: tuple[str, ...] = (
    "accounting_outlook",
    "accounting_period_report",
    "accounting_reports",
    "accounting_results",
    "advances_open",
    "advances_partners",
    "assets_list",
    "bank_movements",
    "bucket_link",
    "cash_book",
    "documents_get",
    "documents_search",
    "partner_balances",
    "partner_statement",
    "payables_due",
    "receivables_overdue",
)


def accounting_roles() -> dict[str, dict[str, Any]]:
    """``{"accountant": ..., "client": ...}`` for ``permissions.roles``."""
    return {
        "accountant": {"tools": ["*"], "workflows": ["*"], "subagents": ["*"]},
        "client": {"tools": list(CLIENT_TOOLS), "workflows": [], "subagents": []},
    }
