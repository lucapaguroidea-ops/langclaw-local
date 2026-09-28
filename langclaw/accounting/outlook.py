"""
Forward-looking facts — what's coming for a client, computed, not guessed.

- :func:`deadlines`: the returns due after a month (D300 / D394 for VAT payers,
  D112 with employees, D100 for micro-enterprises), all on the 25th of the
  following month, monthly or at quarter end per the client's profile.
- :func:`thresholds`: how close the year's revenue is to a limit that changes
  the client's regime (VAT registration for non-payers, the micro-enterprise
  revenue ceiling).
- :func:`trend`: the latest month's VAT against the months before.

An advice workflow (``monthly_advice`` template) hands these to a model that
writes the advice; the model interprets, it doesn't compute. The limits below
are reference data — have your accountant review them and add a row when the
law changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from langclaw.accounting.period import parse_period


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    applies: str  # "vat_non_payer" | "micro"
    year_from: int
    amount: Decimal
    currency: str  # "RON" | "EUR"
    note: str = ""


#: Regime limits by year (the newest row whose year_from ≤ year applies).
LIMITS: tuple[Limit, ...] = (
    Limit("vat_registration", "vat_non_payer", 2025, Decimal(395000), "RON",
          "Codul fiscal art. 310: above it, register for VAT."),
    Limit("micro_revenue", "micro", 2025, Decimal(250000), "EUR",
          "Micro-enterprise revenue ceiling."),
    Limit("micro_revenue", "micro", 2026, Decimal(100000), "EUR",
          "Micro-enterprise revenue ceiling (lowered for 2026)."),
)  # fmt: skip
#: Share of a limit at which it's flagged.
WARN_AT = Decimal(80)
_CENT = Decimal("0.01")


def _next_25th(end: date) -> str:
    year, month = (end.year + 1, 1) if end.month == 12 else (end.year, end.month + 1)
    return date(year, month, 25).isoformat()


def deadlines(period: str, profile: dict[str, Any]) -> list[dict[str, str]]:
    """Returns due for *period* (``YYYY-MM``) given the client's *profile*
    (``vat_payer``, ``vat_period`` monthly/quarterly, ``employees``, ``tax_regime``)."""
    _, end = parse_period(period)
    quarter_end = end.month % 3 == 0
    due = _next_25th(end)
    out: list[dict[str, str]] = []
    if profile.get("vat_payer") and (profile.get("vat_period") != "quarterly" or quarter_end):
        out.append({"form": "D300", "what": "VAT return and payment", "due": due})
        out.append({"form": "D394", "what": "Informative statement of domestic supplies",
                    "due": due})  # fmt: skip
    if profile.get("employees"):
        out.append({"form": "D112", "what": "Payroll contributions and income tax", "due": due})
    if profile.get("tax_regime") == "micro" and quarter_end:
        out.append({"form": "D100", "what": "Micro-enterprise income tax", "due": due})
    return out


def thresholds(revenue_ytd: Decimal, *, year: int, profile: dict[str, Any]) -> list[dict]:
    """How far *revenue_ytd* (RON, net) is into each limit that applies to the client."""
    applies = set()
    if profile.get("vat_payer") is False:
        applies.add("vat_non_payer")
    if profile.get("tax_regime") == "micro":
        applies.add("micro")
    current: dict[str, Limit] = {}
    for limit in LIMITS:
        if limit.applies in applies and limit.year_from <= year:
            if limit.name not in current or limit.year_from > current[limit.name].year_from:
                current[limit.name] = limit
    out = []
    for limit in current.values():
        amount = limit.amount
        if limit.currency == "EUR":
            rate = profile.get("eur_ron")
            if not rate:
                out.append({"name": limit.name, "note": "Set eur_ron on the client's profile "
                            f"to compare with the {limit.amount} EUR limit."})  # fmt: skip
                continue
            amount = (amount * Decimal(str(rate))).quantize(_CENT)
        used = (revenue_ytd * 100 / amount).quantize(Decimal("0.1"))
        out.append({"name": limit.name, "limit": amount, "revenue_ytd": revenue_ytd,
                    "used_pct": used, "warn": used >= WARN_AT, "note": limit.note})  # fmt: skip
    return out


def trend(months: list[tuple[str, Decimal]]) -> dict[str, Any]:
    """The last month's value against the average of the months before it."""
    if not months:
        return {"months": [], "last": None, "average_before": None, "change_pct": None}
    last = months[-1][1]
    before = [v for _, v in months[:-1]]
    avg = (sum(before, Decimal(0)) / len(before)).quantize(_CENT) if before else None
    change = ((last - avg) * 100 / avg).quantize(Decimal("0.1")) if avg else None
    return {"months": [{"period": p, "value": v} for p, v in months], "last": last,
            "average_before": avg, "change_pct": change}  # fmt: skip


_BUCKETS = (("1-30", 30), ("31-60", 60), ("61-90", 90))


def _aging(invoices: list[dict[str, Any]], on: date) -> dict[str, Any]:
    buckets = {name: Decimal(0) for name in ("not_due", "1-30", "31-60", "61-90", "90+")}
    overdue_by: dict[str, list] = {}
    soon = Decimal(0)
    for row in invoices:
        f = row.get("fields") or {}
        gross = abs(Decimal(str(row.get("amount") or 0)))
        if row.get("doc_type") == "credit_note":
            gross = -gross
        else:  # what's left after partial payments
            gross -= Decimal(str(f.get("paid_amount") or 0))
        due = str(f.get("due_date") or "")[:10]
        late = (on - date.fromisoformat(due)).days if due else 0
        if late <= 0:
            buckets["not_due"] += gross
            if due and -late <= 30:
                soon += gross
            continue
        name = next((n for n, days in _BUCKETS if late <= days), "90+")
        buckets[name] += gross
        partner = row.get("receiver" if f.get("direction") == "out" else "sender") or "?"
        entry = overdue_by.setdefault(partner, [Decimal(0), 0])
        entry[0] += gross
        entry[1] += 1
    buckets = {k: v.quantize(_CENT) for k, v in buckets.items()}
    total = sum(buckets.values(), Decimal(0))
    top = sorted(overdue_by.items(), key=lambda kv: kv[1][0], reverse=True)[:5]
    return {
        "total": total,
        "overdue": total - buckets["not_due"],
        "buckets": buckets,
        "top_overdue": [
            {"partner": p, "amount": a.quantize(_CENT), "invoices": n} for p, (a, n) in top
        ],  # fmt: skip
        "_due_30": soon.quantize(_CENT),
    }


def cash_position(
    invoices: list[dict[str, Any]], *, on: date, bank_balance: Decimal | None = None
) -> dict[str, Any]:
    """Receivables and payables aging as of *on* — unpaid invoices only (``paid_on``
    comes from bank matching), at what's still outstanding after partial payments —
    what falls due in the next 30 days, and the bank balance projected over them.
    Overdue amounts aren't counted in the projection."""
    unpaid = [r for r in invoices if not (r.get("fields") or {}).get("paid_on")]
    rec = _aging([r for r in unpaid if (r.get("fields") or {}).get("direction") == "out"], on)
    pay = _aging([r for r in unpaid if (r.get("fields") or {}).get("direction") == "in"], on)
    incoming, outgoing = rec.pop("_due_30"), pay.pop("_due_30")
    return {
        "as_of": on.isoformat(),
        "receivables": rec,
        "payables": pay,
        "next_30_days": {"in": incoming, "out": outgoing},
        "bank_balance": bank_balance,
        "projected_30_days": (
            (bank_balance + incoming - outgoing).quantize(_CENT)
            if bank_balance is not None
            else None
        ),
    }


def overdue_receivables(
    invoices: list[dict[str, Any]], *, on: date, min_days: int = 1
) -> list[dict[str, Any]]:
    """Unpaid sales invoices at least *min_days* past due on *on*, grouped by
    customer (largest outstanding first), each with its invoices (oldest first)."""
    by_partner: dict[str, dict[str, Any]] = {}
    for row in invoices:
        f = row.get("fields") or {}
        if f.get("direction") != "out" or f.get("paid_on") or row.get("doc_type") == "credit_note":
            continue
        due = str(f.get("due_date") or "")[:10]
        if not due:
            continue
        late = (on - date.fromisoformat(due)).days
        left = (Decimal(str(row.get("amount") or 0)) - Decimal(str(f.get("paid_amount") or 0))
                ).quantize(_CENT)  # fmt: skip
        if late < min_days or left <= 0:
            continue
        name = row.get("receiver") or "?"
        entry = by_partner.setdefault(
            name, {"partner": name, "cui": f.get("customer_cui", ""),
                   "email": f.get("customer_email", ""), "outstanding": Decimal(0), "invoices": []}
        )  # fmt: skip
        entry["outstanding"] += left
        entry["invoices"].append(
            {"bucket_key": row.get("bucket_key"), "number": f.get("invoice_number", ""),
             "issued": row.get("document_date"), "due": due, "days_overdue": late,
             "outstanding": left, "reminders_sent": len(f.get("reminders") or []),
             "last_reminder": (f.get("reminders") or [""])[-1]}
        )  # fmt: skip
    for entry in by_partner.values():
        entry["invoices"].sort(key=lambda i: -i["days_overdue"])
    return sorted(by_partner.values(), key=lambda e: e["outstanding"], reverse=True)


def payables_due(
    invoices: list[dict[str, Any]], *, on: date, days: int = 7
) -> list[dict[str, Any]]:
    """Unpaid supplier invoices due by *on* + *days* (overdue ones included), grouped
    by supplier — what to pay, to which IBAN, citing which invoices. Largest first."""
    horizon = on.toordinal() + max(0, days)
    by_partner: dict[str, dict[str, Any]] = {}
    for row in sorted(invoices, key=lambda r: str((r.get("fields") or {}).get("due_date"))):
        f = row.get("fields") or {}
        if f.get("direction") != "in" or f.get("paid_on") or row.get("doc_type") == "credit_note":
            continue
        due = str(f.get("due_date") or "")[:10]
        if not due or date.fromisoformat(due).toordinal() > horizon:
            continue
        left = (Decimal(str(row.get("amount") or 0)) - Decimal(str(f.get("paid_amount") or 0))
                ).quantize(_CENT)  # fmt: skip
        if left <= 0:
            continue
        name = row.get("sender") or "?"
        entry = by_partner.setdefault(name, {
            "partner": name, "cui": f.get("supplier_cui", ""), "iban": f.get("supplier_iban", ""),
            "currency": row.get("currency") or "RON", "amount": Decimal(0), "numbers": [],
            "invoices": [], "overdue": False})  # fmt: skip
        entry["amount"] += left
        entry["numbers"].append(f.get("invoice_number", ""))
        entry["invoices"].append(row.get("bucket_key"))
        entry["overdue"] = entry["overdue"] or date.fromisoformat(due) < on
        entry["iban"] = entry["iban"] or f.get("supplier_iban", "")
    return sorted(by_partner.values(), key=lambda e: e["amount"], reverse=True)
