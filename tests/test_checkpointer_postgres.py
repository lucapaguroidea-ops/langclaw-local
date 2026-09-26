"""Postgres checkpointer survives server-side connection loss.

When Postgres restarts or sleeps (e.g. Railway app sleeping), the server
terminates open connections (``AdminShutdown``). The checkpointer must recover
on the next call instead of failing every message until the gateway restarts.

Integration test: runs only when ``LANGCLAW_TEST_POSTGRES_DSN`` points at a
disposable database, e.g.::

    docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=pw postgres:18
    LANGCLAW_TEST_POSTGRES_DSN=postgresql://postgres:pw@localhost:55432/postgres \\
        uv run pytest tests/test_checkpointer_postgres.py
"""

from __future__ import annotations

import os

import pytest

DSN = os.environ.get("LANGCLAW_TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not DSN, reason="LANGCLAW_TEST_POSTGRES_DSN not set")


async def _terminate_other_connections(dsn: str) -> int:
    """Kill every other connection to the database, as a server restart would."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def test_checkpointer_recovers_after_connections_are_terminated() -> None:
    from langclaw.checkpointer.postgres import PostgresCheckpointerBackend

    config = {"configurable": {"thread_id": "resilience-test", "checkpoint_ns": ""}}

    async with PostgresCheckpointerBackend(DSN) as backend:
        saver = backend.get()
        assert await saver.aget_tuple(config) is None

        assert await _terminate_other_connections(DSN) >= 1

        # Before the fix this raised psycopg.errors.AdminShutdown /
        # OperationalError("the connection is closed") on every later call.
        assert await saver.aget_tuple(config) is None
