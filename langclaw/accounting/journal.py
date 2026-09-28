"""
The journal — posted entries, in the client's own schema, one per document.

The only write path is :meth:`Journal.post`, which re-checks balance inside a
transaction and refuses dates in a closed period (:meth:`Journal.close_period`);
the tables add row-level guards (non-negative amounts, one side per line, one entry per document).
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
CREATE TABLE IF NOT EXISTS {schema}.closed_periods (
    period     TEXT PRIMARY KEY,
    closed_by  TEXT NOT NULL DEFAULT '',
    closed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
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
            entry_date = _date(document.get("document_date"))
            if entry_date is not None:
                period = entry_date.strftime("%Y-%m")
                if await conn.fetchval(
                    f"SELECT 1 FROM {self._schema}.closed_periods WHERE period = $1", period
                ):
                    raise JournalError(f"Period {period} is closed; nothing can be posted in it.")
            entry_id = await conn.fetchval(
                f"INSERT INTO {self._schema}.journal_entries (bucket_key, entry_date, direction, "
                "partner_cui, partner_name, explanation, legal_basis, approved_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
                document["bucket_key"],
                entry_date,
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

    async def reverse(
        self, bucket_key: str, day: date, *, reason: str, approved_by: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """Reverse (stornare) the entry posted for *bucket_key*: post the same lines
        with debit and credit swapped, dated *day*, as ``reverse/<n>/<bucket_key>``,
        and move the original to ``<bucket_key>#reversed-<n>`` so the document can
        be posted again, correctly. Both entries stay in the journal.

        Returns:
            The original's new key and the reversal entry.

        Raises:
            JournalError: nothing posted for *bucket_key*, or *day*'s month closed.
        """
        original = await self.get(bucket_key)
        if original is None:
            raise JournalError(f"Nothing is posted for {bucket_key!r}.")
        pool = await self._db()
        n = 1 + await pool.fetchval(
            f"SELECT count(*) FROM {self._schema}.journal_entries WHERE bucket_key LIKE $1",
            f"{bucket_key}#reversed-%",
        )
        moved = f"{bucket_key}#reversed-{n}"
        rename = f"UPDATE {self._schema}.journal_entries SET bucket_key = $1 WHERE bucket_key = $2"
        await pool.execute(rename, moved, bucket_key)
        note = f"Stornare {bucket_key}: {reason}"
        doc = {"bucket_key": f"reverse/{n}/{bucket_key}", "document_date": day.isoformat(),
               "sender": original["partner_name"], "receiver": original["partner_name"],
               "fields": {"direction": original["direction"],
                          "supplier_cui": original["partner_cui"],
                          "customer_cui": original["partner_cui"]}}  # fmt: skip
        lines = [{"account": x["account"], "debit": x["credit"], "credit": x["debit"],
                  "explanation": note} for x in original["lines"]]  # fmt: skip
        try:
            entry = await self.post(doc, {"lines": lines, "reasoning": note,
                                          "legal_basis": original["legal_basis"]},
                                    approved_by=approved_by)  # fmt: skip
        except JournalError:
            await pool.execute(rename, bucket_key, moved)
            raise
        return moved, entry

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

    async def entries_between(
        self, date_from: date, date_to: date, *, without_invoices: bool = False
    ) -> list[dict[str, Any]]:
        """Entries dated in [*date_from*, *date_to*] with their lines, in date order.
        *without_invoices* keeps only the entries langclaw made itself (bank, cash,
        month close, opening balances, partner offsets: keys under ``bank/``,
        ``cash/``, ``close/``, ``opening/``, ``offset/``)."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT e.id, e.entry_date, e.bucket_key, e.explanation, l.account, l.debit, "
            f"l.credit, l.explanation AS line_explanation FROM {self._schema}.journal_entries e "
            f"JOIN {self._schema}.journal_lines l ON l.entry_id = e.id "
            "WHERE e.entry_date BETWEEN $1 AND $2 AND (NOT $3 OR e.bucket_key LIKE 'bank/%' "
            "OR e.bucket_key LIKE 'cash/%' OR e.bucket_key LIKE 'close/%' "
            "OR e.bucket_key LIKE 'opening/%' OR e.bucket_key LIKE 'offset/%' "
            "OR e.bucket_key LIKE 'reverse/%' OR e.bucket_key LIKE 'advance/%') "
            "ORDER BY e.entry_date, e.id, l.id",
            date_from,
            date_to,
            without_invoices,
        )
        entries: dict[int, dict[str, Any]] = {}
        for r in rows:
            head = {"entry_date": r["entry_date"], "bucket_key": r["bucket_key"],
                    "explanation": r["explanation"], "lines": []}  # fmt: skip
            entry = entries.setdefault(r["id"], head)
            entry["lines"].append({"account": r["account"], "debit": r["debit"],
                                   "credit": r["credit"],
                                   "explanation": r["line_explanation"]})  # fmt: skip
        return list(entries.values())

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

    async def lines_between(
        self, date_from: date, date_to: date, *, without_year_end: bool = False
    ) -> list[dict[str, Any]]:
        """Every posted line with an entry date in [*date_from*, *date_to*];
        *without_year_end* leaves out year-end closing entries (for P&L reports)."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT l.account, l.debit, l.credit FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.entry_date BETWEEN $1 AND $2 "
            "AND NOT ($3 AND e.bucket_key LIKE 'close/%/year-end') ORDER BY l.id",
            date_from,
            date_to,
            without_year_end,
        )
        return [dict(r) for r in rows]

    async def account_lines(self, account: str, date_from: date, date_to: date) -> list[dict]:
        """Lines on *account* (and its analytics) of entries dated in
        [*date_from*, *date_to*], in date order, each with ``counterparts``: the
        entry's accounts on the other side (comma-separated)."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT e.entry_date, e.bucket_key, l.debit, l.credit, l.explanation, "
            f"(SELECT string_agg(DISTINCT o.account, ',') FROM {self._schema}.journal_lines o "
            "WHERE o.entry_id = l.entry_id AND o.id <> l.id AND "
            "((l.debit > 0 AND o.credit > 0) OR (l.credit > 0 AND o.debit > 0))) AS counterparts "
            f"FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.entry_date BETWEEN $1 AND $2 "
            "AND (l.account = $3 OR l.account LIKE $3 || '.%') ORDER BY e.entry_date, l.id",
            date_from,
            date_to,
            account,
        )
        return [dict(r) for r in rows]

    async def balance_until(self, day: date, account: str) -> Decimal:
        """Debit − credit on *account* (and its analytics, e.g. ``4426.01``) for
        entries dated up to *day*."""
        pool = await self._db()
        value = await pool.fetchval(
            f"SELECT COALESCE(SUM(l.debit - l.credit), 0) FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.entry_date <= $1 AND (l.account = $2 OR l.account LIKE $2 || '.%')",
            day,
            account,
        )
        return Decimal(value).quantize(_CENT)

    async def partner_lines(self, partner_cui: str, until: date | None = None) -> list[dict]:
        """Lines on partner accounts (40x / 41x) of entries for *partner_cui*, oldest
        first, up to *until*."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT e.entry_date, e.bucket_key, e.partner_name, l.account, l.debit, l.credit, "
            f"l.explanation FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.partner_cui = $1 AND (l.account LIKE '40%' OR l.account LIKE '41%') "
            "AND ($2::date IS NULL OR e.entry_date <= $2) ORDER BY e.entry_date, l.id",
            partner_cui,
            until,
        )
        return [dict(r) for r in rows]

    async def partner_balances(self, day: date) -> list[dict]:
        """Per partner up to *day*: receivable (41x debit − credit) and payable (40x
        credit − debit)."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT e.partner_cui, max(e.partner_name) AS name, "
            "COALESCE(SUM(CASE WHEN l.account LIKE '41%' THEN l.debit - l.credit END), 0) AS rec, "
            "COALESCE(SUM(CASE WHEN l.account LIKE '40%' THEN l.credit - l.debit END), 0) AS pay "
            f"FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.partner_cui <> '' AND e.entry_date <= $1 "
            "AND (l.account LIKE '40%' OR l.account LIKE '41%') "
            "GROUP BY e.partner_cui ORDER BY e.partner_cui",
            day,
        )
        return [dict(r) for r in rows]

    async def balances_by_name(self, account: str, day: date) -> list[dict]:
        """Debit − credit on *account* per entry partner name, up to *day* (e.g. the
        advances each employee still has to settle on 542)."""
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT e.partner_name AS name, COALESCE(SUM(l.debit - l.credit), 0) AS balance "
            f"FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.entry_date <= $1 AND (l.account = $2 OR l.account LIKE $2 || '.%') "
            "GROUP BY e.partner_name ORDER BY e.partner_name",
            day,
            account,
        )
        return [dict(r) for r in rows]

    async def cash_moved_with(self, partner_cui: str, day: date) -> Decimal:
        """Cash (5311) paid to or received from *partner_cui* on *day*, across all
        of their invoices."""
        pool = await self._db()
        value = await pool.fetchval(
            f"SELECT COALESCE(SUM(l.debit + l.credit), 0) FROM {self._schema}.journal_lines l "
            f"JOIN {self._schema}.journal_entries e ON e.id = l.entry_id "
            "WHERE e.partner_cui = $1 AND e.entry_date = $2 "
            "AND (l.account = '5311' OR l.account LIKE '5311.%')",
            partner_cui,
            day,
        )
        return Decimal(value).quantize(_CENT)

    async def close_period(self, period: str, *, closed_by: str = "") -> bool:
        """Lock *period* (``YYYY-MM``); False when it was already closed."""
        pool = await self._db()
        done = await pool.fetchval(
            f"INSERT INTO {self._schema}.closed_periods (period, closed_by) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            period,
            closed_by,
        )
        return bool(done)

    async def closed_periods(self) -> list[dict[str, Any]]:
        pool = await self._db()
        rows = await pool.fetch(
            f"SELECT period, closed_by, closed_at FROM {self._schema}.closed_periods "
            "ORDER BY period"
        )
        return [{**dict(r), "closed_at": r["closed_at"].isoformat()} for r in rows]

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
