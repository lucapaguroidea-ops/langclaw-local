"""
The journal — posted entries, in the client's own schema, one per document.

The only write path is :meth:`Journal.post`, which re-checks balance inside a
transaction; the tables add row-level guards (non-negative amounts, one side per
line, one entry per document).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langclaw.documents.store import DocumentStore

_CENT = Decimal("0.01")
_TABLES = """
CREATE TABLE IF NOT EXISTS {schema}.journal_entries (
    id            BIGSERIAL PRIMARY KEY,
    bucket_key    TEXT NOT NULL UNIQUE,
    entry_date    DATE,
    direction     TEXT NOT NULL DEFAULT 'in',
    partner_cui   TEXT NOT NULL DEFAULT '',
    partner_name  TEXT NOT NULL DEFAULT '',
    explanation   TEXT NOT NULL DEFAULT '',
    legal_basis   TEXT NOT NULL DEFAULT '',
    approved_by   TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {schema}.journal_lines (
    id          BIGSERIAL PRIMARY KEY,
    entry_id    BIGINT NOT NULL REFERENCES {schema}.journal_entries(id) ON DELETE CASCADE,
    account     TEXT NOT NULL,
    debit       NUMERIC(18, 2) NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit      NUMERIC(18, 2) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    explanation TEXT NOT NULL DEFAULT '',
    CHECK ((debit > 0) <> (credit > 0))
);
CREATE INDEX IF NOT EXISTS journal_partner_idx ON {schema}.journal_entries (partner_cui);
"""


class JournalError(ValueError):
    """The entry can't be posted (unbalanced, already posted...)."""


class Journal:
    """Posted entries next to a client's documents (same schema, same pool)."""

    #: (pool, schema) pairs whose tables exist — a new pool re-checks.
    _ready: set[tuple[int, str]] = set()

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._schema = f'"{store.schema}"'

    async def _db(self) -> Any:
        pool = await self._store._db()
        key = (id(pool), self._store.schema)
        if key not in Journal._ready:
            async with pool.acquire() as conn:
                await conn.execute(_TABLES.replace("{schema}", self._schema))
            Journal._ready.add(key)
        return pool

    async def post(
        self, document: dict[str, Any], proposal: dict[str, Any], *, approved_by: str = ""
    ) -> dict[str, Any]:
        """Post *proposal* for *document* (its ``documents`` row).

        Raises:
            JournalError: unbalanced, empty, or the document already has an entry.
        """
        lines = [
            (
                str(line["account"]).strip(),
                Decimal(str(line.get("debit") or 0)).quantize(_CENT),
                Decimal(str(line.get("credit") or 0)).quantize(_CENT),
                str(line.get("explanation") or ""),
            )
            for line in proposal.get("lines") or []
        ]
        if not lines:
            raise JournalError("An entry needs lines.")
        debit, credit = sum(d for _, d, _, _ in lines), sum(c for _, _, c, _ in lines)
        if debit != credit:
            raise JournalError(f"The entry doesn't balance: debit {debit} ≠ credit {credit}.")
        fields = document.get("fields") or {}
        incoming = fields.get("direction", "in") != "out"
        pool = await self._db()
        async with pool.acquire() as conn, conn.transaction():
            exists = await conn.fetchval(
                f"SELECT 1 FROM {self._schema}.journal_entries WHERE bucket_key = $1",
                document["bucket_key"],
            )
            if exists:
                raise JournalError(f"{document['bucket_key']!r} is already posted.")
            entry_id = await conn.fetchval(
                f"INSERT INTO {self._schema}.journal_entries (bucket_key, entry_date, direction, "
                "partner_cui, partner_name, explanation, legal_basis, approved_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
                document["bucket_key"],
                _date(document.get("document_date")),
                "in" if incoming else "out",
                fields.get("supplier_cui" if incoming else "customer_cui", ""),
                document.get("sender" if incoming else "receiver", ""),
                str(proposal.get("reasoning") or ""),
                str(proposal.get("legal_basis") or ""),
                approved_by,
            )
            await conn.executemany(
                f"INSERT INTO {self._schema}.journal_lines "
                "(entry_id, account, debit, credit, explanation) VALUES ($1, $2, $3, $4, $5)",
                [(entry_id, *line) for line in lines],
            )
        return await self.get(document["bucket_key"])  # type: ignore[return-value]

    async def get(self, bucket_key: str) -> dict[str, Any] | None:
        pool = await self._db()
        entry = await pool.fetchrow(
            f"SELECT * FROM {self._schema}.journal_entries WHERE bucket_key = $1", bucket_key
        )
        if entry is None:
            return None
        lines = await pool.fetch(
            f"SELECT account, debit, credit, explanation FROM {self._schema}.journal_lines "
            "WHERE entry_id = $1 ORDER BY id",
            entry["id"],
        )
        out = dict(entry)
        out["entry_date"] = out["entry_date"].isoformat() if out["entry_date"] else None
        out["created_at"] = out["created_at"].isoformat()
        out["lines"] = [
            {**dict(r), "debit": float(r["debit"]), "credit": float(r["credit"])} for r in lines
        ]
        return out

    async def posted_keys(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT bucket_key FROM {self._schema}.journal_entries "
            "WHERE bucket_key = ANY($1::text[])",
            keys,
        )
        return {r["bucket_key"] for r in rows}

    async def supplier_history(self, partner_cui: str, limit: int = 5) -> list[dict[str, Any]]:
        """How this partner's invoices were booked before: the non-VAT,
        non-partner accounts on the cost side, most used first."""
        if not partner_cui:
            return []
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT l.account, count(*) AS times, max(e.entry_date) AS last_date "
            f"FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.partner_cui = $1 AND l.debit > 0 AND l.account NOT LIKE '442%' "
            "GROUP BY l.account ORDER BY times DESC, last_date DESC LIMIT $2",
            partner_cui,
            limit,
        )
        return [
            {
                "account": r["account"],
                "times": r["times"],
                "last_date": r["last_date"].isoformat() if r["last_date"] else None,
            }
            for r in rows
        ]


def _date(value: Any) -> date | None:
    if not value:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
