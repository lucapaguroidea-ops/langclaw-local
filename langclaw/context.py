from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(kw_only=True)
class LangclawContext:
    """Runtime context schema passed to every agent invocation.

    Centralises channel metadata and RBAC role so middleware, tools,
    and user code can all read ``runtime.context`` uniformly.
    """

    user_role: str = field(default="viewer")
    tenant: str = ""
    """Client (tenant) id the turn runs for — resolved by the gateway from the chat."""
    channel: str = ""
    user_id: str = ""
    context_id: str = ""
    chat_id: str = ""
    metadata: dict = field(default_factory=dict)
