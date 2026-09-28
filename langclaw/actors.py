"""Who is acting — set by langclaw from the channel, never by the model.

A tool that records something (a journal entry, a closed month) reads
:func:`current_actor` instead of trusting an ``approved_by`` argument the
model typed. The gateway sets it for each chat turn (``"telegram:12345"``:
the channel and the sender's id as the channel reports it), and the workflow
runner sets it when a run continues after a review (the reviewer's id, as the
channel that took the answer reports it).

API answers can only name who they are for (the console's shared key doesn't
identify a person), so they are recorded as ``"api:<name>"`` — self-declared.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("langclaw_actor", default="")


def current_actor() -> str:
    """``"<channel>:<user id>"`` of whoever the running code acts for, or ``""``."""
    return _CURRENT.get()


@contextmanager
def actor_scope(actor: str) -> Iterator[str]:
    """Make *actor* current for the enclosed code (a turn, a resumed run)."""
    token = _CURRENT.set(actor or "")
    try:
        yield actor
    finally:
        _CURRENT.reset(token)


def actor_id(channel: str, user_id: str) -> str:
    """The actor for *user_id* on *channel* (``""`` when either is missing)."""
    return f"{channel}:{user_id}" if channel and user_id else ""
