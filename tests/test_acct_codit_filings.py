"""Poarta Primară: CO.DiT per period (WP-11) and filing items (WP-12)."""

from __future__ import annotations

import pytest

from langclaw_acct.catalog import load_catalog
from langclaw_acct.codit import CoDitError, write_codit
from langclaw_acct.filings import FilingError, FilingRegister, filings_for


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def ax(value, certainty="confirmed"):
    return {"value": value, "as_of": "2026-09-01", "source": "ONRC", "certainty": certainty}


def test_payer_gets_exig_default_and_the_years_pins(catalog) -> None:
    c = write_codit(catalog, "1", "2026-09", {"forma": ax("srl"), "impozit": ax("micro_1"),
                                              "tva": ax("tva_platitor")})  # fmt: skip
    assert c.value("exig") == "tva_exig_livrare" and c.axes["exig"].source == "default (T*)"
    assert c.pins["pins_id"] == "pins_ro_2026" and not c.flags


def test_empty_profile_is_not_a_vat_payer(catalog) -> None:
    c = write_codit(catalog, "1", "2026-09", {})
    assert c.value("tva") is None and c.value("exig") is None
    assert [d.form for d in filings_for(catalog, c)] == ["D406"]


@pytest.mark.parametrize(
    ("axes", "rule"),
    [({"tva": ax("tva_neplatitor"), "exig": ax("tva_la_incasare")}, "T1"),
     ({"tva": ax("tva_scutire_mici"), "exig": ax("tva_exig_livrare")}, "T2"),
     ({"forma": ax("srl"), "impozit": ax("pfa_norma")}, "F1"),
     ({"forma": ax("pfa"), "impozit": ax("micro_1")}, "F3"),
     ({"forma": ax("sucursala"), "impozit": ax("micro_1")}, "F5")],
)  # fmt: skip
def test_hard_pairs_refuse_to_save(catalog, axes, rule) -> None:
    with pytest.raises(CoDitError) as err:
        write_codit(catalog, "1", "2026-09", axes)
    assert err.value.rule_id == rule


def test_soft_pairs_flips_and_contested_axes_are_flagged(catalog) -> None:
    prev = write_codit(catalog, "1", "2026-08", {"forma": ax("srl"), "impozit": ax("micro_1")})
    c = write_codit(catalog, "1", "2026-09", {"forma": ax("srl"),
                                              "impozit": ax("profit_16", "contested")},
                    previous=prev)  # fmt: skip
    assert {(f.rule_id, f.code) for f in c.flags} == {("A_CONTESTED", "impozit"),
                                                     ("A_FLIP", "impozit")}  # fmt: skip
    ong = write_codit(catalog, "1", "2026-09", {"forma": ax("ong"), "impozit": ax("profit_16")})
    assert ong.blocks_file and ong.flags[0].rule_id == "F7.1"


def test_filings_follow_the_codit_with_working_day_dates(catalog) -> None:
    c = write_codit(catalog, "1", "2026-09", {
        "forma": ax("srl"), "impozit": ax("micro_1"), "tva": ax("tva_platitor"),
        "tva_period": ax("monthly"), "employees": ax("has")})  # fmt: skip
    due = {d.form: d for d in filings_for(catalog, c)}
    assert due["D300"].due == "2026-10-26"  # 25 Oct 2026 is a Sunday
    assert due["D394"].due == "2026-10-30" and due["D112"].due == "2026-10-26"
    assert due["D100"].due == "2026-10-26"  # September ends a quarter
    assert due["D406"].due is None and "size" in due["D406"].note
    assert "C0_synthetic_parity" in due["D300"].books_gate
    aug = write_codit(catalog, "1", "2026-08", {**{k: ax(v) for k, v in {
        "forma": "srl", "impozit": "micro_1", "tva": "tva_platitor"}.items()}})  # fmt: skip
    forms = {d.form: d for d in filings_for(catalog, aug)}
    assert "D100" not in forms and forms["D300"].due is None
    assert "tva_period" in forms["D300"].note


def test_an_item_closes_only_with_its_receipt(catalog) -> None:
    c = write_codit(catalog, "1", "2026-09", {"tva": ax("tva_platitor"),
                                              "tva_period": ax("monthly")})  # fmt: skip
    reg = FilingRegister()
    reg.open("1", filings_for(catalog, c))
    assert {i.filing_id for i in reg.open_items("1")} >= {"d300_platitor", "d394_platitor"}
    with pytest.raises(FilingError, match="receipt"):
        reg.receipt("1", "2026-09", "d300_platitor", receipt_key=" ", by="ana")
    with pytest.raises(FilingError, match="no open filing"):
        reg.receipt("1", "2026-09", "d112_payroll", receipt_key="r", by="ana")
    done = reg.receipt("1", "2026-09", "d300_platitor", receipt_key="spv/123.pdf", by="ana")
    assert done.state == "filed" and "d300_platitor" not in {
        i.filing_id for i in reg.open_items("1")
    }
