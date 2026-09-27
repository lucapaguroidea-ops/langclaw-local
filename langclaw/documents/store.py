"""
``DocumentStore`` — the ``documents`` table on the data database.

One row per file in the bucket, keyed by ``bucket_key``: saving the same key
again updates the row, so a workflow step that re-runs after a crash never
creates a duplicate. The table is created on first use.

With tenants, each client gets its own schema (``tenant_<id>.documents``) via
:meth:`DocumentStore.for_schema`; every schema shares one connection pool per
database, so 50 clients don't mean 50 pools.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import Any

_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {table} (
    id            BIGSERIAL PRIMARY KEY,
    bucket_key    TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL DEFAULT '',
    mime_type     TEXT NOT NULL DEFAULT '',
    doc_type      TEXT NOT NULL DEFAULT '',
    sender        TEXT NOT NULL DEFAULT '',
    receiver      TEXT NOT NULL DEFAULT '',
    document_date DATE,
    amount        NUMERIC,
    currency      TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    fields        JSONB NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT NOT NULL DEFAULT 'filed',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_sender_idx ON {table} (lower(sender));
CREATE INDEX IF NOT EXISTS documents_receiver_idx ON {table} (lower(receiver));
CREATE INDEX IF NOT EXISTS documents_date_idx ON {table} (document_date);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding REAL[];
"""

#: One asyncpg pool per (database, event loop), shared by every schema.
_POOLS: dict[tuple[str, int], Any] = {}
_LOCKS: dict[int, asyncio.Lock] = {}
_READY: set[tuple[str, int, str]] = set()
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: Cosine similarity between the ``embedding`` column and ``$1`` in plain SQL
#: (no pgvector needed — a linear scan, fine up to tens of thousands of rows).
_COSINE = (
    "(SELECT sum(a * b) / NULLIF(sqrt(sum(a * a)) * sqrt(sum(b * b)), 0) "
    "FROM unnest(embedding, $1::real[]) AS t(a, b))"
)

#: Columns a caller may set (everything else is managed).
COLUMNS = (
    "filename",
    "mime_type",
    "doc_type",
    "sender",
    "receiver",
    "document_date",
    "amount",
    "currency",
    "summary",
    "fields",
    "status",
)


class DocumentStoreError(RuntimeError):
    """A documents-table operation failed (bad input, database unreachable...)."""


class DocumentStore:
    """Async access to the ``documents`` table.

    Args:
        dsn: Postgres connection string (``postgresql://...``).
    """

    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        if not dsn:
            raise DocumentStoreError(
                "No documents database configured: set LANGCLAW__DOCUMENTS__DATABASE_URL "
                "(or DOCUMENTS_DATABASE_URL)."
            )
        if not _SCHEMA_NAME.match(schema):
            raise DocumentStoreError(f"Invalid schema name {schema!r}.")
        self._dsn = dsn
        self.schema = schema
        self._table = f'"{schema}".documents'

    def for_schema(self, schema: str) -> DocumentStore:
        """The same database, another schema (one client's own table)."""
        return DocumentStore(self._dsn, schema=schema)

    @property
    def pool_key(self) -> str:
        """Which connection pool this store uses (shared across schemas)."""
        return self._dsn

    async def _db(self) -> Any:
        loop_key = (self._dsn, id(asyncio.get_running_loop()))
        async with _lock():
            pool = _POOLS.get(loop_key)
            if pool is None:
                try:
                    import asyncpg
                except ImportError as exc:
                    raise DocumentStoreError(
                        "The documents table needs the documents extra: "
                        "uv add 'langclaw[documents]'"
                    ) from exc
                try:
                    pool = await asyncpg.create_pool(self._dsn, min_size=0, max_size=8)
                except Exception as exc:  # noqa: BLE001 — surfaced to tools as text
                    raise DocumentStoreError(f"Cannot open the documents database: {exc}") from exc
                _POOLS[loop_key] = pool
            ready_key = (*loop_key, self.schema)
            if ready_key not in _READY:
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            _SCHEMA.replace("{schema}", f'"{self.schema}"').replace(
                                "{table}", self._table
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    raise DocumentStoreError(f"Cannot open the documents database: {exc}") from exc
                _READY.add(ready_key)
            return pool

    async def close(self) -> None:
        """Close this database's pool (shared by every schema of it)."""
        loop_key = (self._dsn, id(asyncio.get_running_loop()))
        async with _lock():
            pool = _POOLS.pop(loop_key, None)
            _READY.difference_update({k for k in _READY if k[:2] == loop_key})
        if pool is not None:
            await pool.close()

    async def save(self, bucket_key: str, values: dict[str, Any]) -> dict[str, Any]:
        """Insert or update the row for *bucket_key*; returns the stored row.

        Unknown keys go into the ``fields`` JSON (merged), so a workflow can
        store whatever else it extracted without a schema change.
        """
        if not bucket_key:
            raise DocumentStoreError("bucket_key is required.")
        row = {k: v for k, v in values.items() if k in COLUMNS and v is not None}
        extra = {k: v for k, v in values.items() if k not in COLUMNS and k != "bucket_key"}
        fields = {**(row.pop("fields", None) or {}), **extra}
        if "document_date" in row:
            row["document_date"] = _as_date(row["document_date"])
        if "amount" in row:
            row["amount"] = _as_amount(row["amount"])
        names = list(row)
        cols = ", ".join(["bucket_key", *names, "fields"])
        params = ", ".join(f"${i}" for i in range(1, len(names) + 3))
        updates = ", ".join(
            [f"{n} = EXCLUDED.{n}" for n in names]
            + ["fields = d.fields || EXCLUDED.fields", "updated_at = now()"]
        )
        sql = (
            f"INSERT INTO {self._table} AS d ({cols}) VALUES ({params}) "
            f"ON CONFLICT (bucket_key) DO UPDATE SET {updates} RETURNING *"
        )
        pool = await self._db()
        try:
            record = await pool.fetchrow(
                sql, bucket_key, *row.values(), json.dumps(fields, default=str)
            )
        except Exception as exc:  # noqa: BLE001
            raise DocumentStoreError(f"Saving {bucket_key!r} failed: {exc}") from exc
        return _row(record)

    async def get(self, bucket_key: str) -> dict[str, Any] | None:
        pool = await self._db()
        record = await pool.fetchrow(
            f"SELECT * FROM {self._table} WHERE bucket_key = $1", bucket_key
        )
        return _row(record) if record else None

    async def search(
        self,
        *,
        text: str = "",
        sender: str = "",
        receiver: str = "",
        doc_type: str = "",
        date_from: str = "",
        date_to: str = "",
        status: str = "",
        fields: dict[str, str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Filter documents; text filters are case-insensitive substring matches.

        *fields* filters on extracted extras: ``{"jurisdiction": "delaware"}``
        (a dotted key reaches nested values, e.g. ``"notice.days"``).
        """
        where, args = _filters(
            text=text,
            sender=sender,
            receiver=receiver,
            doc_type=doc_type,
            date_from=date_from,
            date_to=date_to,
            status=status,
            fields=fields,
        )
        args.append(max(1, min(int(limit), 200)))
        sql = f"SELECT * FROM {self._table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY document_date DESC NULLS LAST, id DESC LIMIT ${len(args)}"
        pool = await self._db()
        return [_row(r) for r in await pool.fetch(sql, *args)]

    async def set_embedding(self, bucket_key: str, vector: list[float]) -> None:
        pool = await self._db()
        await pool.execute(
            f"UPDATE {self._table} SET embedding = $2::real[] WHERE bucket_key = $1",
            bucket_key,
            list(vector),
        )

    async def similar(
        self, vector: list[float], *, limit: int = 10, **filters: Any
    ) -> list[dict[str, Any]]:
        """Rows ranked by cosine similarity to *vector* (``similarity`` in each row).

        *filters* are the same as :meth:`search`'s (``status``, ``sender``, ...).
        """
        where, args = _filters(**filters, first_param=2)
        where.append("embedding IS NOT NULL")
        args = [list(vector), *args, max(1, min(int(limit), 200))]
        sql = (
            f"SELECT *, {_COSINE} AS similarity FROM {self._table} "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY similarity DESC NULLS LAST, id DESC LIMIT ${len(args)}"
        )
        pool = await self._db()
        rows = []
        for r in await pool.fetch(sql, *args):
            row = _row(r)
            row["similarity"] = round(float(row["similarity"] or 0.0), 4)
            rows.append(row)
        return rows

    async def without_embedding(self, limit: int = 100) -> list[dict[str, Any]]:
        """Rows not embedded yet (saved before semantic search was on)."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT * FROM {self._table} WHERE embedding IS NULL ORDER BY id LIMIT $1", limit
        )
        return [_row(r) for r in rows]

    async def count_without_embedding(self) -> int:
        pool = await self._db()
        return int(
            await pool.fetchval(f"SELECT count(*) FROM {self._table} WHERE embedding IS NULL")
        )

    async def known_keys(self, keys: list[str]) -> set[str]:
        """Which of *keys* already have a row (for scanning the bucket for new files)."""
        if not keys:
            return set()
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT bucket_key FROM {self._table} WHERE bucket_key = ANY($1::text[])", keys
        )
        return {r["bucket_key"] for r in rows}


def _lock() -> asyncio.Lock:
    """The pool-registry lock for the running event loop."""
    return _LOCKS.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())


def _filters(
    *,
    text: str = "",
    sender: str = "",
    receiver: str = "",
    doc_type: str = "",
    date_from: str = "",
    date_to: str = "",
    status: str = "",
    fields: dict[str, str] | None = None,
    first_param: int = 1,
) -> tuple[list[str], list[Any]]:
    """SQL ``WHERE`` clauses + args; placeholders start at ``$first_param``."""
    where: list[str] = []
    args: list[Any] = []

    def add(clause: str, value: Any) -> None:
        args.append(value)
        # every "?" in a clause binds the same parameter
        where.append(clause.replace("?", f"${first_param + len(args) - 1}"))

    if text:
        add("(summary ILIKE ? OR filename ILIKE ? OR fields::text ILIKE ?)", f"%{text}%")
    if sender:
        add("sender ILIKE ?", f"%{sender}%")
    if receiver:
        add("receiver ILIKE ?", f"%{receiver}%")
    if doc_type:
        add("doc_type ILIKE ?", doc_type)
    if date_from:
        add("document_date >= ?", _as_date(date_from))
    if date_to:
        add("document_date <= ?", _as_date(date_to))
    if status:
        add("status = ?", status)
    for key, value in (fields or {}).items():
        if not key or value in (None, ""):
            continue
        args.append(key.split("."))
        path = f"${first_param + len(args) - 1}::text[]"
        add(f"fields #>> {path} ILIKE ?", f"%{value}%")
    return where, args


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise DocumentStoreError(f"Dates must be YYYY-MM-DD, got {value!r}.") from exc


def _as_amount(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        from decimal import Decimal

        return Decimal(str(value).replace(",", ""))
    except Exception as exc:  # noqa: BLE001
        raise DocumentStoreError(f"amount must be a number, got {value!r}.") from exc


def _row(record: Any) -> dict[str, Any]:
    out = dict(record)
    out.pop("embedding", None)  # vectors never go back to the model
    for key in ("document_date", "created_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    if out.get("amount") is not None:
        out["amount"] = float(out["amount"])
    if isinstance(out.get("fields"), str):
        out["fields"] = json.loads(out["fields"])
    return out
