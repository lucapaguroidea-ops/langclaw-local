"""Accounting roles, and the role a client's own staff get in their chat."""

from __future__ import annotations

from types import SimpleNamespace

from langclaw.accounting.roles import CLIENT_TOOLS, accounting_roles
from langclaw.config.schema import DocumentsConfig, LangclawConfig, RoleConfig
from langclaw.tenants import Tenant, tenant_scope


def test_the_client_role_names_only_real_read_only_tools() -> None:
    from langclaw.accounting.tools import build_accounting_tools
    from langclaw.documents import DocumentServices, build_document_tools

    services = DocumentServices(DocumentsConfig())
    real = {t.name for t in [*build_accounting_tools(services), *build_document_tools(services)]}
    assert set(CLIENT_TOOLS) <= real
    writes = {"journal_post", "accounting_period_close", "accounting_period_reopen",
              "journal_reverse", "documents_save", "reminders_file", "bank_import"}  # fmt: skip
    assert not writes & set(CLIENT_TOOLS)
    roles = {name: RoleConfig(**spec) for name, spec in accounting_roles().items()}
    assert roles["accountant"].tools == ["*"] and roles["client"].workflows == []


def _gateway(**permissions):
    from langclaw.gateway.manager import GatewayManager

    config = LangclawConfig()
    config.permissions.enabled = True
    for key, value in permissions.items():
        setattr(config.permissions, key, value)
    config.channels.telegram.user_roles = {"7": "accountant"}
    gateway = GatewayManager.__new__(GatewayManager)
    gateway._config = config
    return gateway


def _msg(user: str):
    return SimpleNamespace(channel="telegram", user_id=user, metadata={})


def test_client_staff_get_the_client_role_in_their_chat() -> None:
    gateway = _gateway(client_role="client", default_role="viewer")
    acme = Tenant(id="acme", name="ACME")
    with tenant_scope(acme):
        assert gateway._resolve_user_role(_msg("99")) == "client"  # someone at ACME
        assert gateway._resolve_user_role(_msg("7")) == "accountant"  # mapped: kept
    assert gateway._resolve_user_role(_msg("99")) == "viewer"  # not a client chat
    plain = _gateway(default_role="viewer")  # no client_role: unchanged behaviour
    with tenant_scope(acme):
        assert plain._resolve_user_role(_msg("99")) == "viewer"
