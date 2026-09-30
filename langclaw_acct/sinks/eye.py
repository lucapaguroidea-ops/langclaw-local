"""
SagaEye — how the graphs read the statutory books (ARCHITECTURE.md §6).

The core graphs depend only on :class:`SagaEye`. Before Mouth-0 (00_LAW §8) the
witness is :class:`RegistruJurnalEye`: the Registru Jurnal the client's own
accounting system exported, parsed by that system's reader. After Mouth-0 a
SAGA report-pack / FDB-replica eye implements the same three calls.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Protocol

from langclaw_acct.sinks.registru_jurnal import RjLine
from langclaw_acct.types import SinkDoc

#: Blocking synthetic accounts in v1 (00_LAW §7).
WATCHED = ("401", "4111", "4426", "4427", "4428", "5121", "5311")


class SagaEye(Protocol):
    def documents(self, cui: str, period: str) -> list[SinkDoc]: ...
    def solduri(self, cui: str, period: str) -> dict[str, str]: ...
    def analytic(self, cui: str, period: str, root: str) -> dict[str, str]: ...


def _synthetic(account: str) -> str:
    return account.split(".")[0]


class RegistruJurnalEye:
    """SagaEye over Registru Jurnal lines, per ``(cui, period)``.

    ``solduri`` is opening + the period's movements, debit positive. A Registru
    Jurnal only carries movements: without *opening* balances (from a balanță
    export) the result is the period's net movement, and ``opening_known``
    says so rather than passing it off as a balance.
    """

    def __init__(self) -> None:
        self._lines: dict[tuple[str, str], list[RjLine]] = {}
        self._opening: dict[tuple[str, str], dict[str, Decimal]] = {}

    def load(self, cui: str, period: str, lines: list[RjLine],
             opening: dict[str, str] | None = None) -> None:  # fmt: skip
        outside = [ln for ln in lines if not ln.date.startswith(period)]
        if outside:
            raise ValueError(f"{len(outside)} line(s) outside {period}, e.g. {outside[0].date}")
        self._lines[(cui, period)] = list(lines)
        if opening is not None:
            self._opening[(cui, period)] = {a: Decimal(v) for a, v in opening.items()}

    def opening_known(self, cui: str, period: str) -> bool:
        return (cui, period) in self._opening

    def _net(self, cui: str, period: str, key) -> dict[str, str]:
        net: dict[str, Decimal] = defaultdict(Decimal)
        for acct, amount in self._opening.get((cui, period), {}).items():
            net[key(acct)] += amount
        for ln in self._lines.get((cui, period), []):
            net[key(ln.debit)] += Decimal(ln.amount)
            net[key(ln.credit)] -= Decimal(ln.amount)
        return {k: str(v.quantize(Decimal("0.01"))) for k, v in sorted(net.items()) if k}

    def solduri(self, cui: str, period: str) -> dict[str, str]:
        return self._net(cui, period, _synthetic)

    def analytic(self, cui: str, period: str, root: str) -> dict[str, str]:
        full = self._net(cui, period, lambda a: a)
        return {a: v for a, v in full.items() if _synthetic(a) == root and a != root}

    def documents(self, cui: str, period: str) -> list[SinkDoc]:
        """One SinkDoc per (document type, number, date); gross = the lines'
        amount on the partner account (4111 / 401) when there is one, else all."""
        groups: dict[tuple[str, str, str], list[RjLine]] = defaultdict(list)
        for ln in self._lines.get((cui, period), []):
            groups[(ln.doc_type, ln.doc_number, ln.date)].append(ln)
        docs = []
        for (doc_type, number, date), lines in groups.items():
            partner = [ln for ln in lines if {_synthetic(ln.debit), _synthetic(ln.credit)}
                       & {"4111", "401"}]  # fmt: skip
            gross = sum((Decimal(ln.amount) for ln in partner or lines), Decimal(0))
            vat = sum((Decimal(ln.amount) for ln in lines
                       if {_synthetic(ln.debit), _synthetic(ln.credit)} & {"4426", "4427"}),
                      Decimal(0))  # fmt: skip
            docs.append(SinkDoc(
                saga_key=f"rj:{doc_type}:{number}:{date}", doc_class=doc_type, number=number,
                date=date, partner_cui=next((x.partner_cui for x in lines if x.partner_cui), None),
                gross=str(gross.quantize(Decimal("0.01"))),
                vat=str(vat.quantize(Decimal("0.01"))) if vat else None,
            ))  # fmt: skip
        return sorted(docs, key=lambda d: (d.date, d.number))
