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


#: The most movements one :meth:`BankBook.list` page returns.
MAX_PAGE = 500


def _where(unmatched_only: bool) -> str:
    if not unmatched_only:
        return ""
    return "WHERE match_kind NOT IN ('certain', 'partial', 'fee', 'cash', 'advance')"


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

    async def list(
        self, *, unmatched_only: bool = False, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """One page of movements (at most 500), newest first; *unmatched_only* keeps
        those not yet applied to an invoice (unmatched or only probable). *offset*
        skips that many, so every movement is reachable; :meth:`totals` counts them."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT * FROM {self._schema}.bank_transactions {_where(unmatched_only)} "
            "ORDER BY booked DESC, key LIMIT $1 OFFSET $2",
            max(1, min(limit, MAX_PAGE)),
            max(0, offset),
        )
        return [
            {**dict(r), "booked": r["booked"].isoformat(), "amount": str(r["amount"]),
             "created_at": r["created_at"].isoformat()}
            for r in rows
        ]  # fmt: skip

    async def totals(self, *, unmatched_only: bool = False) -> dict[str, Any]:
        """How many movements :meth:`list` would page through, and the money in and
        out per currency over all of them:
        ``{"total": 620, "money_in": {"RON": "3100.00"}, "money_out": {"RON": "-1240.00"}}``."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT currency, count(*) AS n, "
            "coalesce(sum(amount) FILTER (WHERE amount > 0), 0) AS money_in, "
            "coalesce(sum(amount) FILTER (WHERE amount < 0), 0) AS money_out "
            f"FROM {self._schema}.bank_transactions {_where(unmatched_only)} "
            "GROUP BY currency ORDER BY count(*) DESC, currency"
        )
        return {
            "total": sum(r["n"] for r in rows),
            "money_in": {r["currency"]: f"{r['money_in']:.2f}" for r in rows if r["money_in"]},
            "money_out": {r["currency"]: f"{r['money_out']:.2f}" for r in rows if r["money_out"]},
        }

    async def unbooked(self, account_iban: str, up_to: date) -> list[dict]:
        """Movements of *account_iban* booked by the bank up to *up_to* that aren't in
        the journal yet (unmatched or only probable) — the expected gap between the
        bank's balance and the ledger's."""
        pool = await self._db()
        where = _where(True) + " AND account_iban = $1 AND booked <= $2"
        rows = await pool.fetch(
            f"SELECT key, booked, amount FROM {self._schema}.bank_transactions {where} "
            "ORDER BY booked, key",
            account_iban,
            up_to,
        )
        return [{"key": r["key"], "booked": r["booked"].isoformat(), "amount": str(r["amount"])}
                for r in rows]  # fmt: skip

    async def get(self, key: str) -> dict[str, Any] | None:
        pool = await self._db()
        row = await pool.fetchrow(
            f"SELECT * FROM {self._schema}.bank_transactions WHERE key = $1", key
        )
        return dict(row) if row else None
