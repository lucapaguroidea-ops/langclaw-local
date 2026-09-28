"""Bank balances carried between statements and checked against the books.

- :func:`statement_chain`: per account, each statement must open with the
  previous one's closing balance (else a statement is missing: ``gap``) and
  start after it ends (else they ``overlap``).
- :func:`reconcile_account`: the bank's closing balance vs the ledger's bank
  account (5121 / 5124 / the profile's ``bank_accounts``) on the same day; the
  difference should be exactly the movements not booked yet.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

_CENT = Decimal("0.01")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT)


def statement_chain(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gaps and overlaps between consecutive statements of the same account.

    Args:
        statements: ``bank_statement`` document rows (``fields``: ``iban``,
            ``date_from``, ``date_to``, ``opening``, ``closing``), any order.
    """
    by_iban: dict[str, list[dict[str, Any]]] = {}
    for st in statements:
        f = st.get("fields") or {}
        if f.get("iban") and f.get("date_from") and f.get("opening") is not None:
            by_iban.setdefault(f["iban"], []).append(st)
    problems = []
    for iban, rows in sorted(by_iban.items()):
        rows.sort(key=lambda r: (r["fields"]["date_from"], r["fields"].get("date_to") or ""))
        for prev, cur in zip(rows, rows[1:], strict=False):
            p, c = prev["fields"], cur["fields"]
            base = {"iban": iban, "after": prev["bucket_key"], "statement": cur["bucket_key"]}
            if c["date_from"] <= (p.get("date_to") or ""):
                message = (
                    f"{cur['bucket_key']} starts {c['date_from']}, before "
                    f"{prev['bucket_key']} ends ({p.get('date_to')}): the same days may "
                    "be imported twice."
                )
                problems.append({**base, "problem": "overlap", "message": message})
            elif _dec(c["opening"]) != _dec(p.get("closing")):
                diff = _dec(c["opening"]) - _dec(p.get("closing"))
                message = (
                    f"{cur['bucket_key']} opens at {_dec(c['opening'])}, but "
                    f"{prev['bucket_key']} closed at {_dec(p.get('closing'))}: a statement "
                    "between them is probably missing."
                )
                problems.append(
                    {**base, "problem": "gap", "difference": str(diff), "message": message}
                )
    return problems


def reconcile_account(
    *,
    iban: str,
    account: str,
    day: str,
    bank: Decimal,
    ledger: Decimal,
    unbooked: list[dict[str, Any]],
    first_opening: Decimal | None = None,
    ledger_at_first: Decimal | None = None,
) -> dict[str, Any]:
    """One bank account on *day*: the statement's closing balance (*bank*) vs the
    ledger account's debit − credit (*ledger*), and how much of the difference
    the *unbooked* movements (with ``amount``) explain. With *first_opening* (the
    earliest statement's opening balance) and *ledger_at_first* (the ledger the
    day before it), a difference there is named as a missing opening balance."""
    difference = _dec(bank) - _dec(ledger)
    pending = sum((_dec(m.get("amount")) for m in unbooked), Decimal(0))
    unexplained = difference - pending
    out = {
        "iban": iban, "account": account, "day": day, "bank": str(_dec(bank)),
        "ledger": str(_dec(ledger)), "difference": str(difference),
        "unbooked": len(unbooked), "unbooked_total": str(pending),
        "unexplained": str(unexplained), "agrees": not difference,
    }  # fmt: skip
    start_gap = (
        _dec(first_opening) - _dec(ledger_at_first)
        if first_opening is not None and ledger_at_first is not None
        else Decimal(0)
    )
    if start_gap:
        out["opening_gap"] = str(start_gap)
    if unexplained and (start_gap or not ledger):
        out["hint"] = (f"The first statement opens at {_dec(first_opening or bank)}, but {account} "
                       f"had {_dec(ledger_at_first or 0)} in the books then: post its opening "
                       "balance with accounting_opening_balances.")  # fmt: skip
    elif unexplained:
        out["hint"] = (
            f"{unexplained} isn't explained by unbooked movements: look for "
            f"entries on {account} without a bank movement, or a missing statement."
        )
    elif difference:
        out["hint"] = f"Book or confirm the {len(unbooked)} open movement(s) (bank_movements)."
    return out
