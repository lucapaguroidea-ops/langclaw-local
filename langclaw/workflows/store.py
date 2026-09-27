"""
The LangGraph ``BaseStore`` behind workflow run records.

:class:`~langclaw.workflows.graph.runs.RunIndex` keeps one record per run (status,
reviews, who answered) in a namespaced key-value store. This module opens that
store with the same backend as the checkpointer — a sibling SQLite file (no
write-lock contention with the checkpointer DB) or the Postgres DSN — as a
lifecycle-managed async context manager, mirroring :mod:`langclaw.checkpointer`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from langgraph.store.base import BaseStore


class WorkflowStoreBackend(ABC):
    """Lifecycle-managed factory for a ``BaseStore`` (use as an async CM).

    Mirrors :class:`langclaw.checkpointer.base.BaseCheckpointerBackend`::

        async with make_workflow_store_backend("sqlite", db_path=path) as backend:
            index = RunIndex(StoreRunIndexBackend(backend.get_store()))
    """

    _store: BaseStore | None = None

    @abstractmethod
    async def _open(self) -> BaseStore:
        """Open the underlying storage and return a ready store."""
        ...

    @abstractmethod
    async def _close(self) -> None:
        """Release resources held by the store."""
        ...

    async def __aenter__(self) -> WorkflowStoreBackend:
        self._store = await self._open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._close()
        self._store = None

    def get_store(self) -> BaseStore:
        """Return the active store (must be called inside the context manager)."""
        if self._store is None:
            raise RuntimeError("Workflow store not initialised — use 'async with backend:' first.")
        return self._store


class MemoryWorkflowStoreBackend(WorkflowStoreBackend):
    """In-process ``InMemoryStore`` backend — durable only within one process."""

    async def _open(self) -> BaseStore:
        from langgraph.store.memory import InMemoryStore

        return InMemoryStore()

    async def _close(self) -> None:
        pass


class _ContextManagedStoreBackend(WorkflowStoreBackend):
    """Backend for stores exposed as an async context manager + ``setup()``.

    Covers both ``AsyncSqliteStore`` and ``AsyncPostgresStore``, whose
    ``from_conn_string`` returns an async CM and whose tables are created by an
    idempotent ``setup()`` call.
    """

    def __init__(self, cm_factory: Any) -> None:
        self._cm_factory = cm_factory
        self._cm: AsyncIterator[BaseStore] | None = None

    async def _open(self) -> BaseStore:
        self._cm = self._cm_factory()
        store = await self._cm.__aenter__()
        await store.setup()  # idempotent CREATE TABLE IF NOT EXISTS
        return store

    async def _close(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._cm = None


def make_workflow_store_backend(
    backend: str,
    *,
    db_path: str = "",
    dsn: str = "",
) -> WorkflowStoreBackend:
    """Return the right :class:`WorkflowStoreBackend` for a config string.

    Args:
        backend: One of ``"memory"``, ``"sqlite"``, or ``"postgres"``.
        db_path: SQLite file path (``~`` expanded).  Use a path *distinct* from
            the checkpointer's DB file to avoid SQLite write-lock contention
            between two connections.
        dsn:     Postgres connection string (shares fine with the checkpointer).

    Raises:
        ValueError: for an unknown backend name.
    """
    if backend == "memory":
        return MemoryWorkflowStoreBackend()
    if backend == "sqlite":
        from langgraph.store.sqlite import AsyncSqliteStore

        path = str(Path(db_path).expanduser())
        return _ContextManagedStoreBackend(lambda: AsyncSqliteStore.from_conn_string(path))
    if backend == "postgres":
        from langgraph.store.postgres import AsyncPostgresStore

        return _ContextManagedStoreBackend(lambda: AsyncPostgresStore.from_conn_string(dsn))
    raise ValueError(
        f"Unknown workflow store backend: {backend!r}. Choose 'memory', 'sqlite', or 'postgres'."
    )
