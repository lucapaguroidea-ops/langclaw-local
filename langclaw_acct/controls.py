"""
WP-08 — Layer 1: the three buckets, the named controls, and PeriodDiff.

The left side is always the expected set (approved Jobs) plus explained rules;
the right side is what the witness (:class:`SagaEye`) shows. Never an internal
journal (ArticoleControls.forbidden).

- :func:`buckets` puts every sink document in ``expected`` (it matches a Job:
  number + date, gross within 0.05), ``explained_sink_only`` (a rule names it)
  or ``unexplained``; Jobs with no sink document are outbound holes.
- :func:`run_controls` runs every row of ``ArticoleControls``. Each control
  that has a check here returns PASS / FAIL; a control this code can't
  evaluate yet (its inputs don't exist) returns INFO with why — never PASS.
- :func:`period_diff` is the verdict: ``material`` when there's a hole, an
  unexplained sink document, a watched account off by ≥ 0.01 or a blocking
  FAIL. Nothing downstream (Jev included) can clear it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from langclaw_acct.catalog import Catalog
from langclaw_acct.sinks.eye import WATCHED, SagaEye
from langclaw_acct.types import BucketRow, ControlRun, ExpectedItem, PeriodDiff, SinkDoc

MATCH_TOLERANCE = Decimal("0.05")
_CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class ExplainedRule:
    """Tags sink documents the expected set won't have (``POST /rules``)."""

    rule_id: str
    doc_class: str | None = None
    number_prefix: str | None = None

    def matches(self, doc: SinkDoc) -> bool:
        if self.doc_class and doc.doc_class != self.doc_class:
            return False
        if self.number_prefix and not doc.number.startswith(self.number_prefix):
            return False
        return bool(self.doc_class or self.number_prefix)


def _q(v: Decimal) -> str:
    return str(v.quantize(_CENT))


def buckets(
    expected: list[ExpectedItem], sink: list[SinkDoc], rules: list[ExplainedRule] = ()
) -> tuple[list[BucketRow], list[str]]:
    """Bucket rows for every sink document, and the outbound holes (job ids)."""
    open_items = list(expected)
    rows: list[BucketRow] = []
    for doc in sink:
        hit = next(
            (
                e
                for e in open_items
                if e.number == doc.number
                and e.date == doc.date
                and abs(Decimal(e.gross) - Decimal(doc.gross)) <= MATCH_TOLERANCE
            ),
            None,
        )
        if hit is not None:
            open_items.remove(hit)
            rows.append(
                BucketRow(
                    kind="expected",
                    expected=hit,
                    sink=doc,
                    delta_gross=_q(Decimal(doc.gross) - Decimal(hit.gross)),
                )
            )
            continue
        rule = next((r for r in rules if r.matches(doc)), None)
        rows.append(
            BucketRow(
                kind="explained_sink_only" if rule else "unexplained",
                sink=doc,
                rule_id=rule.rule_id if rule else None,
                delta_gross=doc.gross,
            )
        )
    return rows, [e.job_id for e in open_items]


@dataclass
class ControlInputs:
    """Everything the controls read for one client-month."""

    cui: str
    period: str
    eye: SagaEye
    rows: list[BucketRow]
    holes: list[str]
    profile: dict[str, Any] = field(default_factory=dict)
    expected_balances: dict[str, str] | None = None
    """Expected + explained movement per watched synthetic account, when known."""
    prefile_statuses: list[str] = field(default_factory=list)
    """Statuses of the Jobs about to be packaged (prefile layer)."""


Check = Callable[[dict[str, Any], ControlInputs], ControlRun]


def _run(c: dict, status: str, target: str, actual: str, diff: str = "") -> ControlRun:
    return ControlRun(
        control_id=c["control_id"], status=status, target=target, actual=actual, diff=diff
    )


def _info(c: dict, why: str) -> ControlRun:
    return _run(c, "INFO", "n/a", why)


def _c0(c: dict, x: ControlInputs) -> ControlRun:
    if x.expected_balances is None:
        return _info(c, "expected movement per account not available yet")
    sink = x.eye.solduri(x.cui, x.period)
    eps = Decimal(c.get("epsilon", "0.01"))
    off = {
        a: Decimal(sink.get(a, "0")) - Decimal(x.expected_balances.get(a, "0"))
        for a in c.get("watched") or WATCHED
    }
    bad = {a: _q(d) for a, d in off.items() if abs(d) >= eps}
    return _run(c, "FAIL" if bad else "PASS", "Δ < " + str(eps), f"{len(bad)} off", str(bad))


def _c1(c: dict, x: ControlInputs) -> ControlRun:
    return _run(c, "FAIL" if x.holes else "PASS", "0 holes", str(len(x.holes)), ", ".join(x.holes))


def _c2(c: dict, x: ControlInputs) -> ControlRun:
    bad = [r.sink.saga_key for r in x.rows if r.kind == "unexplained" and r.sink]
    return _run(c, "FAIL" if bad else "PASS", "0 unexplained", str(len(bad)), ", ".join(bad))


def _tie(c: dict, x: ControlInputs) -> ControlRun:
    root = (c.get("watched") or [""])[0]
    analytic = x.eye.analytic(x.cui, x.period, root)
    if not analytic:
        return _info(c, f"no analytics under {root} (maps not set yet)")
    total = sum((Decimal(v) for v in analytic.values()), Decimal(0))
    synthetic = Decimal(x.eye.solduri(x.cui, x.period).get(root, "0"))
    diff = synthetic - total
    ok = abs(diff) < Decimal(c.get("epsilon", "0.01"))
    return _run(c, "PASS" if ok else "FAIL", _q(synthetic), _q(total), _q(diff))


def _regime_4428(c: dict, x: ControlInputs) -> ControlRun:
    if x.profile.get("tva") in (None, "tva_platitor"):
        return _run(c, "PASS", "n/a", "VAT payer or regime unknown")
    bal = Decimal(x.eye.solduri(x.cui, x.period).get("4428", "0"))
    return _run(c, "FAIL" if bal else "PASS", "0.00", _q(bal))


def _regime_442x(c: dict, x: ControlInputs) -> ControlRun:
    if x.profile.get("tva") in (None, "tva_platitor"):
        return _run(c, "PASS", "n/a", "VAT payer or regime unknown")
    sink = x.eye.solduri(x.cui, x.period)
    moved = {a: sink[a] for a in ("4423", "4424") if Decimal(sink.get(a, "0"))}
    return _run(c, "FAIL" if moved else "PASS", "no 4423/4424", str(moved))


def _dup(c: dict, x: ControlInputs) -> ControlRun:
    dup = x.prefile_statuses.count("already_in_sink")
    return _run(c, "FAIL" if dup else "PASS", "0", str(dup))


#: control_id → check. Rows without an entry report INFO, never PASS.
CHECKS: dict[str, Check] = {
    "C0_synthetic_parity": _c0,
    "C1_outbound_complete": _c1,
    "C2_unexplained_empty": _c2,
    "M1_1_payables_tie": _tie,
    "M1_2_receivables_tie": _tie,
    "M1_1_trade_ext": lambda c, x: _info(c, "advisory; needs maps for 403–409"),
    "M1_2_trade_ext": lambda c, x: _info(c, "advisory; needs maps for 411–419"),
    "T_regime_4428": _regime_4428,
    "T_regime_442x": _regime_442x,
    "P_prefile_duplicate": _dup,
}


def run_controls(catalog: Catalog, x: ControlInputs, layer: str = "v2") -> list[ControlRun]:
    """Every control of *layer* (``v2`` / ``prefile``; ``both`` rows run in either)."""
    out: list[ControlRun] = []
    for cid, c in catalog.rows["ArticoleControls"].items():
        if c.get("layer") not in (layer, "both") or cid == "P_prefile_hard_failures":
            continue
        need = c.get("require") or {}
        if any(x.profile.get(axis) not in vals for axis, vals in need.items()):
            out.append(_run(c, "PASS", "n/a", f"not applicable: needs {need}"))
            continue
        check = CHECKS.get(cid)
        out.append(check(c, x) if check else _info(c, "no check wired for this control yet"))
    if layer == "prefile":
        hard = hard_failures(catalog, out)
        c = catalog.rows["ArticoleControls"]["P_prefile_hard_failures"]
        out.append(_run(c, "FAIL" if hard else "PASS", "0", str(hard)))
    return out


def hard_failures(catalog: Catalog, runs: list[ControlRun]) -> int:
    controls = catalog.rows["ArticoleControls"]
    return sum(
        1
        for r in runs
        if r.status == "FAIL" and controls[r.control_id].get("severity") == "blocking"
    )


def period_diff(
    catalog: Catalog, x: ControlInputs, snapshot_id: str
) -> tuple[PeriodDiff, list[ControlRun]]:
    """The month's verdict plus the control runs behind it."""
    runs = run_controls(catalog, x)
    hard = hard_failures(catalog, runs)
    unexplained = [r for r in x.rows if r.kind == "unexplained"]
    sink = x.eye.solduri(x.cui, x.period)
    synthetic_delta: dict[str, str] = {}
    if x.expected_balances is not None:
        synthetic_delta = {
            a: _q(Decimal(sink.get(a, "0")) - Decimal(x.expected_balances.get(a, "0")))
            for a in WATCHED
        }
    watched_off = [a for a, d in synthetic_delta.items() if abs(Decimal(d)) >= _CENT]
    blockers = (
        [f"outbound hole: {j}" for j in x.holes]
        + [f"unexplained in books: {r.sink.saga_key}" for r in unexplained if r.sink]
        + [f"{a} off by {synthetic_delta[a]}" for a in watched_off]
        + [
            f"{r.control_id} FAIL"
            for r in runs
            if r.status == "FAIL"
            and catalog.rows["ArticoleControls"][r.control_id].get("severity") == "blocking"
        ]
    )
    diff = PeriodDiff(
        outbound_holes=x.holes,
        inbound=[r for r in x.rows if r.kind != "expected"],
        synthetic_delta=synthetic_delta,
        material=bool(blockers),
        blockers=blockers,
        snapshot_id=snapshot_id,
        hard_failures=hard,
    )
    return diff, runs


def may_file(diff: PeriodDiff) -> bool:
    """V2 ``file`` is possible only on a non-material month with no hard failure."""
    return not diff.material and diff.hard_failures == 0
