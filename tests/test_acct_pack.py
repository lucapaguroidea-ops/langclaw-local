"""Poarta Primară first slice: WP-00 types, WP-01 catalog loader, the RJ eye."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from langclaw_acct.catalog import CATALOG_DIR, CatalogError, UnknownId, load_catalog
from langclaw_acct.sinks import RegistruJurnalEye, read_registru_jurnal
from langclaw_acct.sinks.registru_jurnal import RjFormatError
from langclaw_acct.types import ExpectedItem, JobRecord, TenantRef, can_move

# --- WP-00 types -----------------------------------------------------------


def test_models_refuse_extra_keys() -> None:
    with pytest.raises(ValidationError, match="extra"):
        TenantRef(cui="RO1", surprise=True)


def test_money_and_dates_are_strings_of_the_right_shape() -> None:
    item = ExpectedItem(job_id="j", doc_class="factura", number="1", date="2026-09-01",
                        partner_cui=None, gross="121.00", net="100", vat="21.00")  # fmt: skip
    assert item.gross == "121.00"
    with pytest.raises(ValidationError, match="valid string"):
        ExpectedItem(**{**item.model_dump(), "gross": 121.0})
    with pytest.raises(ValidationError, match="money"):
        ExpectedItem(**{**item.model_dump(), "gross": "1.234,50"})
    with pytest.raises(ValidationError, match="fiscal date"):
        ExpectedItem(**{**item.model_dump(), "date": "01.09.2026"})


def test_job_status_machine_moves_forward_only() -> None:
    job = JobRecord(job_id="j", tenant_cui="RO1", source_hash="h", articol_id="a",
                    schema_version="1")  # fmt: skip
    job = job.moved("extracted").moved("bound").moved("reconcile_pre").moved("approved")
    with pytest.raises(ValueError, match="approved → acked"):
        job.moved("acked")
    assert job.moved("failed").status == "failed"
    assert can_move("acked", "reopened") and not can_move("acked", "packaged")


# --- WP-01 catalog ---------------------------------------------------------


def test_the_shipped_catalog_loads_and_holds_together() -> None:
    cat = load_catalog()
    assert cat.articol("ro_efactura_inbound")["graph_id"] == "ingest_source_doc"
    assert cat.articol("recon_pre_standard")  # Reconcile rows are articole too
    assert "control_disposition" in cat.rows["ArticoleHITL"]  # additive file merged
    assert "extras_statement_pdf" in cat.rows["ArticoleSourceDoc"]
    assert cat.hitl_kind("which_cui", graph_id="folder_triage")
    assert all(m["status"] != "active" for m in cat.rows["ArticoleWriteModule"].values())


def test_unknown_ids_are_refused_not_skipped() -> None:
    cat = load_catalog()
    with pytest.raises(UnknownId, match="define_articol"):
        cat.articol("invented_by_jev")
    with pytest.raises(UnknownId):
        cat.hitl_kind("made_up_kind")
    with pytest.raises(UnknownId, match="not allowed"):
        cat.hitl_kind("which_cui", graph_id="monthly_close")


def _copy(tmp_path: Path) -> Path:
    dest = tmp_path / "catalog"
    shutil.copytree(CATALOG_DIR, dest)
    return dest


def _edit(path: Path, fn) -> None:
    doc = yaml.safe_load(path.read_text())
    fn(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True))


def test_flux_listing_a_module_that_doesnt_list_it_back_is_drift(tmp_path: Path) -> None:
    root = _copy(tmp_path)

    def add(doc: dict) -> None:
        row = next(r for r in doc["flux"] if r["articol_id"] == "storno_iesire")
        row["write_modules"].append("nota_nc_dbf")

    _edit(root / "30_cale/ARTICOLE_FLUX_v1.yaml", add)
    with pytest.raises(CatalogError, match="drift: flux storno_iesire → nota_nc_dbf"):
        load_catalog(root)


def test_active_module_needs_a_green_fixture(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    _edit(root / "30_cale/ARTICOLE_WRITE_MODULE_v1.yaml",
          lambda d: d["modules"][0].update(status="active"))  # fmt: skip
    with pytest.raises(CatalogError, match="parteneri_xml is active without"):
        load_catalog(root)


def test_duplicate_additive_id_and_bad_yaml_fail_the_load(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    _edit(root / "60_harvest/ARTICOLE_HITL_ADD_v1.yaml",
          lambda d: d["kinds"].append({"kind": "which_cui"}))  # fmt: skip
    with pytest.raises(CatalogError, match="duplicate ArticoleHITL id 'which_cui'"):
        load_catalog(root)
    root2 = _copy(tmp_path / "b")
    (root2 / "10_lege_firma/ARTICOLE_PINS_v1.yaml").write_text("pins: [\n)")
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_catalog(root2)


# --- SagaEye over a Registru Jurnal ----------------------------------------

RJ = """data,nr_document,fel_document,explicatie,cont_debit,cont_credit,suma,cui_partener
2026-09-03,F101,factura_iesire,Vanzare,4111.00001,704,"1.000,00",RO2
2026-09-03,F101,factura_iesire,TVA,4111.00001,4427,210.00,RO2
2026-09-10,E7,extras,Incasare F101,5121,4111.00001,1210,RO2
2026-09-12,A5,factura_intrare,Chirie,612,401.00009,500,RO3
2026-09-12,A5,factura_intrare,TVA,4426,401.00009,105,RO3
"""


def test_generic_rj_reads_amounts_in_either_notation() -> None:
    lines = read_registru_jurnal("generic_csv", RJ.encode())
    assert [ln.amount for ln in lines][:3] == ["1000.00", "210.00", "1210.00"]
    assert lines[0].partner_cui == "RO2"
    with pytest.raises(RjFormatError, match="missing columns"):
        read_registru_jurnal("generic_csv", b"data,suma\n2026-09-01,1\n")


def test_a_system_without_a_real_export_has_no_reader_yet() -> None:
    with pytest.raises(RjFormatError, match="send one"):
        read_registru_jurnal("saga", b"")


def test_eye_over_rj_gives_documents_balances_and_analytics() -> None:
    eye = RegistruJurnalEye()
    eye.load("RO1", "2026-09", read_registru_jurnal("generic_csv", RJ.encode()),
             opening={"5121": "300.00"})  # fmt: skip
    docs = {d.number: d for d in eye.documents("RO1", "2026-09")}
    assert docs["F101"].gross == "1210.00" and docs["F101"].vat == "210.00"
    assert docs["A5"].gross == "605.00" and docs["A5"].partner_cui == "RO3"
    bal = eye.solduri("RO1", "2026-09")
    assert bal["5121"] == "1510.00" and bal["4111"] == "0.00" and bal["401"] == "-605.00"
    assert eye.analytic("RO1", "2026-09", "401") == {"401.00009": "-605.00"}
    assert eye.opening_known("RO1", "2026-09")


def test_eye_refuses_lines_from_another_month() -> None:
    lines = read_registru_jurnal("generic_csv", RJ.encode())
    with pytest.raises(ValueError, match="outside 2026-08"):
        RegistruJurnalEye().load("RO1", "2026-08", lines)
