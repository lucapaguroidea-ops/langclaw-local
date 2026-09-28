"""Statement continuity and bank ↔ ledger reconciliation (pure functions)."""

from __future__ import annotations

from decimal import Decimal

from langclaw.accounting.bank.reconcile import reconcile_account, statement_chain


def _st(key: str, iban: str, start: str, end: str, opening: str, closing: str) -> dict:
    return {"bucket_key": key, "fields": {"iban": iban, "date_from": start, "date_to": end,
                                          "opening": opening, "closing": closing}}  # fmt: skip


def test_a_continuous_chain_has_no_problems() -> None:
    statements = [
        _st("b", "RO1", "2026-02-01", "2026-02-28", "150", "90"),
        _st("a", "RO1", "2026-01-01", "2026-01-31", "100", "150"),
        _st("x", "RO2", "2026-01-01", "2026-01-31", "0", "7"),
    ]
    assert statement_chain(statements) == []


def test_a_missing_statement_and_an_overlap_are_flagged() -> None:
    statements = [
        _st("jan", "RO1", "2026-01-01", "2026-01-31", "100", "150"),
        _st("mar", "RO1", "2026-03-01", "2026-03-31", "180", "200"),
        _st("mar2", "RO1", "2026-03-15", "2026-04-10", "200", "210"),
    ]
    problems = statement_chain(statements)
    assert [p["problem"] for p in problems] == ["gap", "overlap"]
    assert problems[0]["iban"] == "RO1" and problems[0]["after"] == "jan"
    assert problems[0]["statement"] == "mar" and problems[0]["difference"] == "30.00"
    assert "150.00" in problems[0]["message"] and "180.00" in problems[0]["message"]
    assert problems[1]["statement"] == "mar2"


def test_reconcile_explains_the_difference_with_unbooked_movements() -> None:
    out = reconcile_account(
        iban="RO1", account="5121", day="2026-01-31", bank=Decimal("150"),
        ledger=Decimal("130"), unbooked=[{"key": "k1", "amount": "20.00"}],
    )  # fmt: skip
    assert out["difference"] == "20.00" and out["unbooked_total"] == "20.00"
    assert out["unexplained"] == "0.00" and out["agrees"] is False
    ok = reconcile_account(iban="RO1", account="5121", day="2026-01-31",
                           bank=Decimal("150"), ledger=Decimal("150"), unbooked=[])  # fmt: skip
    assert ok["agrees"] is True and ok["difference"] == "0.00"
    odd = reconcile_account(iban="RO1", account="5121", day="2026-01-31",
                            bank=Decimal("150"), ledger=Decimal("0"), unbooked=[])  # fmt: skip
    assert odd["unexplained"] == "150.00" and "opening" in odd["hint"]
