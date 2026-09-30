"""Poarta Primară: buckets, controls, PeriodDiff (WP-08)."""

from __future__ import annotations

import pytest

from langclaw_acct.catalog import load_catalog
from langclaw_acct.controls import (
    ControlInputs,
    ExplainedRule,
    buckets,
    may_file,
    period_diff,
    run_controls,
)  # fmt: skip
from langclaw_acct.sinks import RegistruJurnalEye, read_registru_jurnal
from langclaw_acct.types import ExpectedItem

HEAD = "data,nr_document,fel_document,explicatie,cont_debit,cont_credit,suma\n"
RJ = HEAD + (
    "2026-09-10,A9,factura_intrare,Servicii,628,401.00001,100\n"
    "2026-09-10,A9,factura_intrare,TVA,4426,401.00001,21\n"
    "2026-09-30,C1,comision,Comision banca,627,5121,5\n"
)


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def _eye(text: str = RJ) -> RegistruJurnalEye:
    eye = RegistruJurnalEye()
    eye.load("1", "2026-09", read_registru_jurnal("generic_csv", text.encode()))
    return eye


def _exp(number: str = "A9", gross: str = "121.00", date: str = "2026-09-10") -> ExpectedItem:
    return ExpectedItem(job_id=f"j-{number}", doc_class="factura_intrare", number=number,
                        date=date, partner_cui="2", gross=gross, net="100", vat="21")  # fmt: skip


def _inputs(eye, expected, rules=(), **kw) -> ControlInputs:
    rows, holes = buckets(expected, eye.documents("1", "2026-09"), list(rules))
    return ControlInputs(cui="1", period="2026-09", eye=eye, rows=rows, holes=holes, **kw)


def test_buckets_match_explain_or_leave_unexplained() -> None:
    eye = _eye()
    rows, holes = buckets([_exp(), _exp("F404")], eye.documents("1", "2026-09"),
                          [ExplainedRule("bank_fees", doc_class="comision")])  # fmt: skip
    kinds = {r.sink.number: (r.kind, r.rule_id) for r in rows}
    assert kinds == {"A9": ("expected", None), "C1": ("explained_sink_only", "bank_fees")}
    assert holes == ["j-F404"]
    rows, _ = buckets([_exp()], eye.documents("1", "2026-09"))
    assert [r.kind for r in rows if r.sink.number == "C1"] == ["unexplained"]


def test_a_clean_month_may_file(catalog) -> None:
    x = _inputs(_eye(), [_exp()], [ExplainedRule("fees", doc_class="comision")])
    diff, runs = period_diff(catalog, x, snapshot_id="s1")
    status = {r.control_id: r.status for r in runs}
    assert status["C1_outbound_complete"] == status["C2_unexplained_empty"] == "PASS"
    assert status["M1_1_payables_tie"] == "PASS"  # 401.00001 sums to 401
    assert status["C0_synthetic_parity"] == "INFO"  # expected movement not known: not a PASS
    assert status["M1_8_4428_open"] == "PASS"  # not applicable: client isn't on încasare
    assert not diff.material and may_file(diff)


def test_holes_and_unexplained_make_the_month_material(catalog) -> None:
    diff, _ = period_diff(catalog, _inputs(_eye(), [_exp(), _exp("F404")]), snapshot_id="s")
    assert diff.material and not may_file(diff) and diff.hard_failures == 2
    assert "outbound hole: j-F404" in diff.blockers
    assert "unexplained in books: rj:comision:C1:2026-09-30" in diff.blockers


def test_watched_account_off_by_a_cent_is_material(catalog) -> None:
    expected = {"401": "-121.00", "4426": "21.00", "5121": "-5.01"}
    x = _inputs(_eye(), [_exp()], [ExplainedRule("fees", doc_class="comision")],
                expected_balances=expected)  # fmt: skip
    diff, runs = period_diff(catalog, x, snapshot_id="s")
    assert diff.synthetic_delta["5121"] == "0.01" and "5121 off by 0.01" in diff.blockers
    assert next(r for r in runs if r.control_id == "C0_synthetic_parity").status == "FAIL"


def test_non_payer_with_4428_fails_the_regime_control(catalog) -> None:
    eye = _eye(HEAD + "2026-09-05,F1,factura_iesire,TVA neexigibila,4111,4428,19\n")
    runs = run_controls(catalog, _inputs(eye, [], profile={"tva": "tva_neplatitor"}))
    assert {r.control_id: r.status for r in runs}["T_regime_4428"] == "FAIL"


def test_prefile_layer_refuses_packaging_a_duplicate(catalog) -> None:
    x = _inputs(_eye(), [_exp()], prefile_statuses=["approved", "already_in_sink"])
    runs = {r.control_id: r for r in run_controls(catalog, x, layer="prefile")}
    assert runs["P_prefile_duplicate"].status == "FAIL"
    assert runs["P_prefile_hard_failures"].status == "FAIL"
    assert set(runs) == {"P_prefile_duplicate", "P_prefile_hard_failures"}


def test_every_v2_control_reports_something(catalog) -> None:
    runs = run_controls(catalog, _inputs(_eye(), [_exp()]))
    v2 = [c for c, r in catalog.rows["ArticoleControls"].items() if r["layer"] in ("v2", "both")]
    assert sorted(r.control_id for r in runs) == sorted(v2)
