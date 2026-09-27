"""Per-client bank movements, next to the journal (same schema, same pool)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from langclaw.accounting.journal import Journal

if TYPE_CHECKING:
    from langclaw.accounting.bank.parse import Transaction
    from langclaw.documents.store import DocumentStore

_TABLE = """
CREATE TABLE IF NOT EXISTS {schema}.bank_transactions (
    key           TEXT PRIMARY KEY,
    statement_key TEXT NOT NULL,
    account_iban  TEXT NOT NULL DEFAULT '',
    booked        DATE NOT NULL,
    amount        NUMERIC(18, 2) NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'RON',
    reference     TEXT NOT NULL DEFAULT '',
    counterparty  TEXT NOT NULL DEFAULT '',
    iban          TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    matched_key   TEXT NOT NULL DEFAULT '',
    match_kind    TEXT NOT NULL DEFAULT '',
    match_because TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bank_booked_idx ON {schema}.bank_transactions (booked);
"""


class BankBook:
    """Imported bank movements for one client; importing twice adds nothing."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._schema = f'"{store.schema}"'

    async def _db(self) -> Any:
        pool = await self._store._db()
        key = (id(pool), f"{self._store.schema}:bank")
        if key not in Journal._ready:  # one table cache for all accounting tables
            async with pool.acquire() as conn:
                await conn.execute(_TABLE.replace("{schema}", self._schema))
            Journal._ready.add(key)
        return pool

    async def add(self, statement_key: str, account_iban: str, txs: list[Transaction]) -> set[str]:
        """Store *txs*; returns the keys that were new."""
        pool = await self._db()
        new: set[str] = set()
        async with pool.acquire() as conn, conn.transaction():
            for t in txs:
                inserted = await conn.fetchval(
                    f"INSERT INTO {self._schema}.bank_transactions (key, statement_key, "
                    "account_iban, booked, amount, currency, reference, counterparty, iban, "
                    "description) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
                    "ON CONFLICT (key) DO NOTHING RETURNING key",
                    t.key, statement_key, account_iban, date.fromisoformat(t.booked), t.amount,
                    t.currency, t.reference, t.counterparty, t.iban, t.description,
                )  # fmt: skip
                if inserted:
                    new.add(t.key)
        return new

    async def set_match(self, key: str, bucket_key: str, kind: str, because: str) -> bool:
        pool = await self._db()
        done = await pool.fetchval(
            f"UPDATE {self._schema}.bank_transactions SET matched_key = $2, match_kind = $3, "
            "match_because = $4 WHERE key = $1 RETURNING key",
            key,
            bucket_key,
            kind,
            because,
        )
        return bool(done)

    async def list(self, *, unmatched_only: bool = False, limit: int = 50) -> list[dict]:
        """Movements, newest first; *unmatched_only* keeps those without a certain match."""
        pool = await self._db()
        where = "WHERE match_kind <> 'certain'" if unmatched_only else ""
        rows = await pool.fetch(
            f"SELECT * FROM {self._schema}.bank_transactions {where} "
            "ORDER BY booked DESC, key LIMIT $1",
            max(1, min(limit, 500)),
        )
        return [
            {**dict(r), "booked": r["booked"].isoformat(), "amount": str(r["amount"]),
             "created_at": r["created_at"].isoformat()}
            for r in rows
        ]  # fmt: skip

    async def get(self, key: str) -> dict[str, Any] | None:
        pool = await self._db()
        row = await pool.fetchrow(
            f"SELECT * FROM {self._schema}.bank_transactions WHERE key = $1", key
        )
        return dict(row) if row else None
