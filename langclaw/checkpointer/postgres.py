"""
PostgreSQL checkpointer backend — for production / multi-process deployments.

Requires: langclaw[postgres]
    pip install langclaw[postgres]
    # or:
    uv add "langclaw[postgres]"
"""

from __future__ import annotations

from typing import Any

from langclaw.checkpointer.base import BaseCheckpointerBackend


class PostgresCheckpointerBackend(BaseCheckpointerBackend):
    """
    Async PostgreSQL-backed LangGraph checkpoint saver.

    Connections come from a pool that health-checks each one before handing it
    out, so a server restart (or a platform putting the database to sleep)
    costs a reconnect instead of failing every message until the gateway is
    restarted.

    Args:
        dsn: PostgreSQL connection string.
             Example: ``postgresql://user:pass@localhost:5432/langclaw``
        max_pool_size: Upper bound on pooled connections.
    """

    def __init__(self, dsn: str, max_pool_size: int = 10) -> None:
        if not dsn:
            raise ValueError(
                "PostgresCheckpointerBackend requires a non-empty DSN. "
                "Set checkpointer.postgres.dsn in your config or "
                "LANGCLAW__CHECKPOINTER__POSTGRES__DSN env var."
            )
        self._dsn = dsn
        self._max_pool_size = max_pool_size
        self._pool: Any = None

    async def _open(self) -> object:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool
        except ImportError as exc:
            raise ImportError(
                "PostgreSQL checkpointer requires 'langclaw[postgres]'. "
                "Install with: uv add 'langclaw[postgres]'"
            ) from exc

        # Same connection settings AsyncPostgresSaver.from_conn_string uses.
        self._pool = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=1,
            max_size=self._max_pool_size,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        await self._pool.open(wait=True)
        saver = AsyncPostgresSaver(self._pool)
        # Run migrations on first use
        await saver.setup()
        return saver

    async def _close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
