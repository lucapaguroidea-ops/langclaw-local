"""
Fixed assets — a per-client register and linear depreciation.

Depreciation starts the month after an asset is put into service (Codul fiscal
art. 28), is ``value / life_months`` a month rounded to the ban, and the last
month takes the rounding so the total is exactly the value. The month's close
posts one entry: D 6811 / C the accumulated-depreciation account of each asset's
class (2131 → 2813, 214 → 2814, 205 → 2805).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from langclaw.accounting.journal import Journal
from langclaw.accounting.period import parse_period

if TYPE_CHECKING:
    from langclaw.documents.store import DocumentStore

_CENT = Decimal("0.01")
EXPENSE_ACCOUNT = "6811"


@dataclass(frozen=True, slots=True)
class Asset:
    id: int
    name: str
    account: str  # 20x / 21x
    value: Decimal
    in_service: str  # ISO date
    life_months: int


def depreciation_account(account: str) -> str:
    """The accumulated-depreciation account for fixed-asset *account*.

    Raises:
        ValueError: not a 20x / 21x account.
    """
    base = account.split(".", 1)[0]
    if len(base) < 3 or base[:2] not in ("20", "21"):
        raise ValueError(f"{account!r} isn't a fixed-asset account (2xx: 20x or 21x).")
    return f"28{base[1]}{base[2]}"


def _month_index(day: date) -> int:
    return day.year * 12 + day.month - 1


def monthly_depreciation(asset: Asset, period: str) -> Decimal:
    """What *asset* depreciates in *period* (``YYYY-MM``)."""
    start = _month_index(date.fromisoformat(asset.in_service[:10])) + 1
    month = _month_index(parse_period(period)[0])
    n = month - start + 1  # 1-based month of depreciation
    if n < 1 or n > asset.life_months:
        return Decimal(0)
    rate = (asset.value / asset.life_months).quantize(_CENT)
    if n == asset.life_months:
        return (asset.value - rate * (asset.life_months - 1)).quantize(_CENT)
    return rate


def depreciation_entry(assets: list[Asset], period: str) -> dict[str, Any] | None:
    """The month's depreciation entry for *assets*, or ``None`` when nothing is due."""
    by_account: dict[str, Decimal] = {}
    names: list[str] = []
    for asset in assets:
        amount = monthly_depreciation(asset, period)
        if amount:
            key = depreciation_account(asset.account)
            by_account[key] = by_account.get(key, Decimal(0)) + amount
            names.append(asset.name)
    if not by_account:
        return None
    total = sum(by_account.values(), Decimal(0))
    note = f"Amortizare {period}"
    lines = [{"account": EXPENSE_ACCOUNT, "debit": str(total), "credit": "0", "explanation": note}]
    lines += [
        {"account": acc, "debit": "0", "credit": str(amount), "explanation": note}
        for acc, amount in sorted(by_account.items())
    ]
    return {
        "lines": lines,
        "reasoning": f"{note}: {', '.join(names)} (liniar).",
        "legal_basis": "Codul fiscal art. 28; OMFP 1802/2014",
    }


_TABLE = """
CREATE TABLE IF NOT EXISTS {schema}.fixed_assets (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    account     TEXT NOT NULL,
    value       NUMERIC(18, 2) NOT NULL CHECK (value > 0),
    in_service  DATE NOT NULL,
    life_months INTEGER NOT NULL CHECK (life_months > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class FixedAssets:
    """A client's asset register (same schema and pool as the journal)."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._schema = f'"{store.schema}"'

    async def _db(self) -> Any:
        pool = await self._store._db()
        key = (id(pool), f"{self._store.schema}:assets")
        if key not in Journal._ready:
            async with pool.acquire() as conn:
                await conn.execute(_TABLE.replace("{schema}", self._schema))
            Journal._ready.add(key)
        return pool

    async def add(
        self, name: str, account: str, value: Decimal, in_service: str, life_months: int
    ) -> Asset:
        """Register an asset.

        Raises:
            ValueError: bad account, value, date or life.
        """
        depreciation_account(account)
        if value <= 0 or life_months <= 0:
            raise ValueError("Value and life (months) must be positive.")
        pool = await self._db()
        row = await pool.fetchrow(
            f"INSERT INTO {self._schema}.fixed_assets (name, account, value, in_service, "
            "life_months) VALUES ($1, $2, $3, $4, $5) RETURNING *",
            name, account, value.quantize(_CENT), date.fromisoformat(in_service[:10]), life_months,
        )  # fmt: skip
        return _asset(row)

    async def list(self) -> list[Asset]:
        pool = await self._db()
        rows = await pool.fetch(f"SELECT * FROM {self._schema}.fixed_assets ORDER BY id")
        return [_asset(r) for r in rows]


def _asset(row: Any) -> Asset:
    return Asset(
        id=row["id"],
        name=row["name"],
        account=row["account"],
        value=Decimal(row["value"]),
        in_service=row["in_service"].isoformat(),
        life_months=row["life_months"],
    )
