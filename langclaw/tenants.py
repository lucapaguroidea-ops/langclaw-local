"""
Tenants — clients whose data must never mix.

A langclaw deployment can serve several clients (e.g. an accounting firm's client
companies). With ``LANGCLAW__TENANTS__ENABLED=true``:

- each chat belongs to at most one client (:class:`Tenant.chats`), resolved by the
  gateway from where a message came from — never from the model, never from
  message metadata a user could set;
- the resolved client is the *current tenant* (:func:`current_tenant`) for the
  agent turn or workflow run, and is stored on the run so a resumed run keeps it;
- tenant-aware tools (the document tools) only reach that client's data — its own
  bucket prefix and database schema — and refuse when there is no client.

Clients live in a :class:`TenantRegistry` on langclaw's store (the same database
as workflow runs) and are managed from the console's Clients page or
``/v1/tenants``.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from langclaw.naming import check_tenant_id

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

_NAMESPACE = ("langclaw_tenants",)
_PAGE = 100


def chat_ref(channel: str, chat_id: str) -> str:
    """The ``"channel:chat_id"`` form chats are listed in (e.g. ``telegram:-100123``)."""
    return f"{channel}:{chat_id}"


def _check_chat(value: str) -> str:
    value = (value or "").strip()
    channel, sep, chat = value.partition(":")
    if not (sep and channel.strip() and chat.strip()):
        raise ValueError(
            f"Chat {value!r} must be written channel:chat_id, e.g. telegram:-1001234567890."
        )
    return f"{channel.strip()}:{chat.strip()}"


class Tenant(BaseModel):
    """One client: who they are, which chats are theirs, and their company context."""

    id: str
    """Short slug (``acme``): used in the bucket prefix and database schema name."""
    name: str
    """Display name, e.g. ``ACME SOLUTIONS SRL``."""
    tax_id: str = ""
    """Fiscal code (CUI / CIF), e.g. ``RO12345678``."""
    chats: list[str] = Field(default_factory=list)
    """Chats that belong to this client, as ``channel:chat_id``."""
    review_chat: str = ""
    """Where this client's review requests also go (``channel:chat_id``)."""
    profile: dict[str, Any] = Field(default_factory=dict)
    """Company context workflows can ground decisions in (VAT regime, CAEN, ...)."""
    created_at: str = ""
    updated_at: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return check_tenant_id(value)

    @field_validator("chats")
    @classmethod
    def _chats(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for chat in value:
            chat = _check_chat(chat)
            if chat not in out:
                out.append(chat)
        return out

    @field_validator("review_chat")
    @classmethod
    def _review_chat(cls, value: str) -> str:
        return _check_chat(value) if value.strip() else ""

    def review_target(self) -> dict[str, str] | None:
        """The review chat as a delivery target, or ``None``."""
        if not self.review_chat:
            return None
        channel, _, chat = self.review_chat.partition(":")
        return {"channel": channel, "user_id": chat, "context_id": chat, "chat_id": chat}


class TenantRegistry:
    """Clients, stored in a LangGraph ``BaseStore``; chat lookups are cached."""

    def __init__(self, store: BaseStore) -> None:
        self._store = store
        self._by_chat: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    async def list(self) -> list[Tenant]:
        items: list[Tenant] = []
        offset = 0
        while True:
            page = await self._store.asearch(_NAMESPACE, limit=_PAGE, offset=offset)
            items.extend(Tenant.model_validate(item.value) for item in page)
            if len(page) < _PAGE:
                return sorted(items, key=lambda t: t.id)
            offset += _PAGE

    async def get(self, tenant_id: str) -> Tenant | None:
        item = await self._store.aget(_NAMESPACE, tenant_id)
        return Tenant.model_validate(item.value) if item is not None else None

    async def save(self, tenant: Tenant) -> Tenant:
        """Create or update *tenant*.

        Raises:
            ValueError: one of its chats already belongs to another client.
        """
        async with self._lock:
            by_chat = await self._chat_map()
            for chat in tenant.chats:
                owner = by_chat.get(chat)
                if owner is not None and owner != tenant.id:
                    raise ValueError(f"Chat {chat!r} is already linked to client {owner!r}.")
            existing = await self.get(tenant.id)
            stamp = datetime.now(UTC).isoformat(timespec="seconds")
            saved = tenant.model_copy(
                update={
                    "created_at": existing.created_at if existing else stamp,
                    "updated_at": stamp,
                }
            )
            await self._store.aput(_NAMESPACE, saved.id, saved.model_dump())
            self._by_chat = None
            return saved

    async def delete(self, tenant_id: str) -> bool:
        async with self._lock:
            if await self.get(tenant_id) is None:
                return False
            await self._store.adelete(_NAMESPACE, tenant_id)
            self._by_chat = None
            return True

    async def for_chat(self, channel: str, chat_id: str) -> Tenant | None:
        """The client a chat belongs to, or ``None``."""
        tenant_id = (await self._chat_map()).get(chat_ref(channel, chat_id))
        return await self.get(tenant_id) if tenant_id else None

    async def _chat_map(self) -> dict[str, str]:
        if self._by_chat is None:
            self._by_chat = {chat: t.id for t in await self.list() for chat in t.chats}
        return self._by_chat


# -- the current tenant ---------------------------------------------------------------

_CURRENT: contextvars.ContextVar[Tenant | None] = contextvars.ContextVar(
    "langclaw_current_tenant", default=None
)


def current_tenant() -> Tenant | None:
    """The client of the agent turn or workflow run in progress, if any."""
    return _CURRENT.get()


@contextmanager
def tenant_scope(tenant: Tenant | None) -> Iterator[Tenant | None]:
    """Make *tenant* current for the enclosed code (turn, run, intake)."""
    token = _CURRENT.set(tenant)
    try:
        yield tenant
    finally:
        _CURRENT.reset(token)
