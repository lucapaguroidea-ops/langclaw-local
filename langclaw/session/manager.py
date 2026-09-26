"""
SessionManager — maps (channel, user_id, context_id) → LangGraph thread_id.

``context_id`` is a session discriminator, **not** a delivery address.
Different values create separate LangGraph threads for the same user
(e.g. ``"cron:task:<uuid>"`` isolates a scheduled task from the main
conversation).

Conversation state lives entirely inside the LangGraph checkpointer.
This manager only maintains the ID mapping so the same thread is resumed
across messages from the same user in the same context.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import Any

from loguru import logger

# Namespace for deterministic thread ids (uuid5 of the conversation key).
_THREAD_NAMESPACE = uuid.UUID("6f1c3b2e-6a4d-5c8e-9b0a-1d2e3f4a5b6c")


class SessionManager:
    """
    Thread-safe mapping of channel conversation keys to LangGraph thread IDs.

    Key format: ``"<channel>:<user_id>:<context_id>"``

    The mapping is held in-process by default. For multi-process / multi-instance
    deployments extend this class to back the store with Redis or a shared DB —
    the interface stays identical.
    """

    def __init__(self, checkpointer: Any | None = None) -> None:
        # Optional LangGraph saver; when set, /reset deletes the thread's
        # checkpoints so the (stable) thread id starts empty again.
        self._checkpointer = checkpointer
        self._store: dict[str, str] = {}
        self._active_agent_store: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_thread(
        self,
        channel: str,
        user_id: str,
        context_id: str = "default",
    ) -> str:
        """
        Return the existing thread_id for this (channel, user, context) triple,
        or create and store a new UUID thread_id.
        """
        key = self._make_key(channel, user_id, context_id)
        async with self._lock:
            if key not in self._store:
                # Deterministic: the same conversation maps to the same thread
                # after a restart, so its checkpointed history is found again.
                self._store[key] = str(uuid.uuid5(_THREAD_NAMESPACE, key))
                logger.info(f"Using thread {self._store[key]} for {key}")
            return self._store[key]

    async def delete_thread(
        self,
        channel: str,
        user_id: str,
        context_id: str = "default",
    ) -> bool:
        """
        Reset a conversation (e.g. on /reset). Returns True if it existed.

        Thread ids are deterministic, so forgetting the mapping alone would
        resume the same thread; with a checkpointer attached the thread's
        checkpoints are deleted so the next message starts fresh.
        """
        key = self._make_key(channel, user_id, context_id)
        thread_id = str(uuid.uuid5(_THREAD_NAMESPACE, key))
        async with self._lock:
            existed = self._store.pop(key, None) is not None
        if self._checkpointer is not None:
            result = self._checkpointer.adelete_thread(thread_id)
            if inspect.isawaitable(result):
                await result
            return True
        return existed

    def make_runnable_config(
        self,
        thread_id: str,
        channel_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Build a LangGraph ``RunnableConfig`` dict for the given thread.

        The optional ``channel_context`` dict is forwarded into
        ``configurable["channel_context"]`` where ``ChannelContextMiddleware``
        picks it up.
        """
        configurable: dict[str, Any] = {"thread_id": thread_id}
        if channel_context:
            configurable["channel_context"] = channel_context
        return {"configurable": configurable}

    async def get_config(
        self,
        channel: str,
        user_id: str,
        context_id: str = "default",
        channel_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Convenience: get or create thread then return a ready RunnableConfig.
        """
        thread_id = await self.get_or_create_thread(channel, user_id, context_id)
        logger.info(f"Current thread_id: {thread_id} for {channel}:{user_id}:{context_id}")
        return self.make_runnable_config(thread_id, channel_context)

    async def get_active_agent(self, channel: str, user_id: str) -> str:
        """Return the active agent name for this (channel, user_id) pair.

        Returns:
            The stored agent name, or ``"default"`` if none has been set.
        """
        key = f"{channel}:{user_id}"
        async with self._lock:
            return self._active_agent_store.get(key, "default")

    async def set_active_agent(self, channel: str, user_id: str, agent_name: str) -> None:
        """Persist the active agent name for this (channel, user_id) pair.

        Passing ``"default"`` removes the entry, keeping the store clean.

        Args:
            channel:    Channel name (e.g. ``"telegram"``).
            user_id:    Platform-specific user identifier.
            agent_name: Agent name to activate. Pass ``"default"`` to reset.
        """
        key = f"{channel}:{user_id}"
        async with self._lock:
            if agent_name == "default":
                self._active_agent_store.pop(key, None)
            else:
                self._active_agent_store[key] = agent_name

    def all_threads(self) -> dict[str, str]:
        """Return a snapshot of all key→thread_id mappings (for diagnostics)."""
        return dict(self._store)

    @staticmethod
    def _make_key(channel: str, user_id: str, context_id: str) -> str:
        return f"{channel}:{user_id}:{context_id}"
