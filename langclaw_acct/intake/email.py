"""
Email intake — two addresses per client, routed by the address mail was
delivered to, never by what it says.

``<client>@<firm domain>``         from the client: untrusted. The text is data
                                   to classify; nothing it says is followed.
``<client>-intern@<firm domain>``  from the firm's own staff: files to process.

Both are aliases / groups of one ingestion mailbox (no mailbox per client).
:func:`admit` decides, from headers only, which client, how far to trust the
message, and whether it's quarantined for a person. The mailbox reader
(Gmail API) is a :class:`MailSource`; :class:`FakeMailbox` stands in for tests
until the Workspace credentials exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Trust = Literal["client", "internal"]


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class EmailMessage:
    message_id: str
    delivered_to: str
    from_addr: str
    subject: str
    body: str
    attachments: tuple[Attachment, ...] = ()
    spf_pass: bool = False
    dkim_pass: bool = False


@dataclass(frozen=True, slots=True)
class ClientMailbox:
    """A client's addresses: *slug* is the local part, *cui* the tenant."""

    slug: str
    cui: str
    contacts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Admission:
    client: ClientMailbox | None
    trust: Trust | None
    accepted: bool
    reason: str = ""
    """Why it's quarantined (for the person who reviews it)."""

    @property
    def may_emit(self) -> bool:
        """Files may become Jobs without a person first (internal mail only)."""
        return self.accepted and self.trust == "internal"


@dataclass
class MailRouter:
    firm_domain: str
    clients: dict[str, ClientMailbox] = field(default_factory=dict)
    internal_suffix: str = "-intern"

    def add(self, client: ClientMailbox) -> None:
        if client.slug.endswith(self.internal_suffix):
            raise ValueError(f"client slug can't end in {self.internal_suffix!r}")
        self.clients[client.slug] = client

    def route(self, recipient: str) -> tuple[ClientMailbox | None, Trust | None]:
        local, _, domain = recipient.strip().lower().partition("@")
        if domain != self.firm_domain.lower():
            return None, None
        trust: Trust = "client"
        if local.endswith(self.internal_suffix):
            local, trust = local[: -len(self.internal_suffix)], "internal"
        client = self.clients.get(local)
        return (client, trust) if client else (None, None)

    def admit(self, msg: EmailMessage) -> Admission:
        client, trust = self.route(msg.delivered_to)
        if client is None:
            return Admission(None, None, False, f"no client at {msg.delivered_to}")
        sender = msg.from_addr.strip().lower()
        authentic = msg.spf_pass and msg.dkim_pass
        if trust == "internal":
            if not sender.endswith("@" + self.firm_domain.lower()):
                return Admission(client, trust, False, f"{sender} is not firm staff")
            if not authentic:
                return Admission(client, trust, False, "sender not authenticated (SPF/DKIM)")
            return Admission(client, trust, True)
        if sender not in {c.lower() for c in client.contacts}:
            return Admission(client, trust, False, f"{sender} is not a listed contact")
        if not authentic:
            return Admission(client, trust, False, "sender not authenticated (SPF/DKIM)")
        return Admission(client, trust, True)


class MailSource(Protocol):
    def fetch(self) -> list[EmailMessage]: ...


class FakeMailbox:
    """In-memory :class:`MailSource`: what's put in comes out once."""

    def __init__(self) -> None:
        self._queue: list[EmailMessage] = []

    def put(self, msg: EmailMessage) -> None:
        self._queue.append(msg)

    def fetch(self) -> list[EmailMessage]:
        out, self._queue = self._queue, []
        return out
