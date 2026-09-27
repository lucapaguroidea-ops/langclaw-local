"""e-Factura (RO): UBL parsing, dummy invoices, SPV clients, per-client sync."""

from __future__ import annotations

from decimal import Decimal

import pytest

from langclaw.documents.efactura.samples import Party, make_invoice
from langclaw.documents.efactura.ubl import UblError, parse_ubl

SELLER = Party(
    name="Enel Energie SA",
    cui="RO22000460",
    reg_com="J40/1234/2007",
    iban="RO49AAAA1B31007593840000",
)
BUYER = Party(name="ACME SOLUTIONS SRL", cui="RO12345678")

# Structure of the Ministry of Finance CIUS-RO example (eInvoice_ex.xml), trimmed.
MF_STYLE = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:efactura.mfinante.ro:CIUS-RO:1.0.1</cbc:CustomizationID>
  <cbc:ID>6422451356</cbc:ID>
  <cbc:IssueDate>2022-05-31</cbc:IssueDate>
  <cbc:DueDate>2022-06-30</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>RON</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>Seller SRL</cbc:Name></cac:PartyName>
    <cac:PartyTaxScheme><cbc:CompanyID>RO1234567890</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity><cbc:RegistrationName>Seller SRL</cbc:RegistrationName>
      <cbc:CompanyLegalForm>J40/12345/1998</cbc:CompanyLegalForm></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyTaxScheme><cbc:CompanyID>RO987456123</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity>
      <cbc:RegistrationName>Buyer SRL</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:PaymentMeans><cbc:PaymentMeansCode>31</cbc:PaymentMeansCode>
    <cac:PayeeFinancialAccount><cbc:ID>RO80RNCB0067054355123456</cbc:ID></cac:PayeeFinancialAccount>
  </cac:PaymentMeans>
  <cac:TaxTotal><cbc:TaxAmount currencyID="RON">2094.22</cbc:TaxAmount>
    <cac:TaxSubtotal><cbc:TaxableAmount currencyID="RON">696.12</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="RON">34.79</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>5.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>
    <cac:TaxSubtotal><cbc:TaxableAmount currencyID="RON">22875.45</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="RON">2059.43</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>9.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="RON">23571.57</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="RON">23571.57</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="RON">25665.79</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="RON">25665.79</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine><cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">2</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="RON">696.12</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>item name</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>5</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="RON">348.06</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>"""


def test_parses_the_ministry_example_structure() -> None:
    inv = parse_ubl(MF_STYLE.encode())
    assert inv.kind == "invoice" and inv.number == "6422451356"
    assert (inv.issue_date, inv.due_date, inv.currency) == ("2022-05-31", "2022-06-30", "RON")
    assert (inv.supplier.name, inv.supplier.cui, inv.supplier.reg_com) == (
        "Seller SRL",
        "RO1234567890",
        "J40/12345/1998",
    )
    assert (inv.customer.name, inv.customer.cui) == ("Buyer SRL", "RO987456123")
    assert inv.supplier.iban == "RO80RNCB0067054355123456"
    assert inv.total_net == Decimal("23571.57") and inv.total_vat == Decimal("2094.22")
    assert inv.total_gross == Decimal("25665.79")
    assert [(v.rate, v.taxable, v.vat) for v in inv.vat_breakdown] == [
        (Decimal("5.00"), Decimal("696.12"), Decimal("34.79")),
        (Decimal("9.00"), Decimal("22875.45"), Decimal("2059.43")),
    ]
    (line,) = inv.lines
    assert (line.name, line.quantity, line.unit, line.net, line.vat_rate) == (
        "item name",
        Decimal("2"),
        "C62",
        Decimal("696.12"),
        Decimal("5"),
    )


def test_generated_invoices_round_trip_with_current_rates() -> None:
    xml = make_invoice(
        number="ENEL-2026-0915",
        issue_date="2026-09-15",
        supplier=SELLER,
        customer=BUYER,
        lines=[("Energie electrica august", 1, 250.00, 21), ("Abonament", 1, 20.00, 21)],
    )
    inv = parse_ubl(xml)
    assert inv.number == "ENEL-2026-0915" and inv.customer.cui == "RO12345678"
    assert inv.total_net == Decimal("270.00") and inv.total_vat == Decimal("56.70")
    assert inv.total_gross == Decimal("326.70")
    assert [(v.rate, v.vat) for v in inv.vat_breakdown] == [(Decimal("21.00"), Decimal("56.70"))]
    assert inv.check() == []  # totals add up


def test_mixed_rates_and_a_credit_note() -> None:
    mixed = parse_ubl(
        make_invoice(
            number="F1",
            issue_date="2026-09-01",
            supplier=SELLER,
            customer=BUYER,
            lines=[("Carte", 2, 50, 11), ("Birotica", 1, 100, 21)],
        )
    )
    assert sorted((v.rate, v.vat) for v in mixed.vat_breakdown) == [
        (Decimal("11.00"), Decimal("11.00")),
        (Decimal("21.00"), Decimal("21.00")),
    ]
    credit = parse_ubl(
        make_invoice(
            number="CN1",
            issue_date="2026-09-20",
            supplier=SELLER,
            customer=BUYER,
            lines=[("Storno", 1, 100, 21)],
            credit_note=True,
        )
    )
    assert credit.kind == "credit_note" and credit.total_gross == Decimal("121.00")


def test_inconsistent_totals_are_reported_not_hidden() -> None:
    bad = MF_STYLE.replace(
        '<cbc:TaxInclusiveAmount currencyID="RON">25665.79',
        '<cbc:TaxInclusiveAmount currencyID="RON">99999.99',
    )
    problems = parse_ubl(bad.encode()).check()
    assert problems and "25665.79" in problems[0]


def test_not_an_invoice_is_a_clear_error() -> None:
    with pytest.raises(UblError, match="not a UBL Invoice or CreditNote"):
        parse_ubl(b"<Order xmlns='urn:x'/>")
    with pytest.raises(UblError, match="not valid XML"):
        parse_ubl(b"%PDF-1.4")


# -- SPV clients -------------------------------------------------------------------------

import io  # noqa: E402
import json  # noqa: E402
import zipfile  # noqa: E402

import httpx  # noqa: E402

from langclaw.documents.efactura.spv import (  # noqa: E402
    AnafSpvClient,
    DemoSpvClient,
    SpvError,
    unzip_invoice,
)


async def test_demo_spv_serves_realistic_invoices_for_the_client() -> None:
    spv = DemoSpvClient(invoices=5)
    messages = await spv.list_messages("12345678", days=60)
    assert messages == await DemoSpvClient(invoices=5).list_messages("12345678", days=60)
    assert {m.kind for m in messages} <= {"received", "sent"} and len(messages) == 5
    received = next(m for m in messages if m.kind == "received")
    archive = await spv.download(received.id)
    names = sorted(zipfile.ZipFile(io.BytesIO(archive)).namelist())
    assert names == [f"{received.id}.xml", f"semnatura_{received.id}.xml"]  # like ANAF's zip
    inv = parse_ubl(unzip_invoice(archive))
    assert inv.customer.cui.endswith("12345678") and inv.check() == []
    assert {v.rate for v in inv.vat_breakdown} <= {Decimal("21.00"), Decimal("11.00")}
    other = await DemoSpvClient(invoices=5).list_messages("87654321", days=60)
    assert {m.id for m in other}.isdisjoint({m.id for m in messages})


def _anaf(handler) -> AnafSpvClient:
    return AnafSpvClient("tok123", transport=httpx.MockTransport(handler))


async def test_anaf_client_lists_and_downloads() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/listaMesajeFactura"):
            return httpx.Response(
                200,
                json={
                    "mesaje": [
                        {
                            "data_creare": "202609150930",
                            "cif": "12345678",
                            "id_solicitare": "5001",
                            "detalii": (
                                "Factura cu id_incarcare=5001 emisa de cif_emitent=22000460 "
                                "pentru cif_beneficiar=12345678"
                            ),
                            "tip": "FACTURA PRIMITA",
                            "id": "3001",
                        },
                        {
                            "data_creare": "202609160800",
                            "cif": "12345678",
                            "id_solicitare": "5002",
                            "detalii": "Erori de validare",
                            "tip": "ERORI FACTURA",
                            "id": "3002",
                        },
                    ],
                    "serial": "abc",
                    "cui": "12345678",
                    "titlu": "Lista Mesaje disponibile",
                },
            )
        return httpx.Response(
            200, content=b"PK-zip-bytes", headers={"content-type": "application/zip"}
        )

    spv = _anaf(handler)
    msgs = await spv.list_messages("RO12345678", days=90)
    assert [(m.id, m.kind, m.created) for m in msgs] == [
        ("3001", "received", "2026-09-15T09:30"),
        ("3002", "error", "2026-09-16T08:00"),
    ]
    assert seen[0].url.path == "/prod/FCTEL/rest/listaMesajeFactura"
    assert (
        seen[0].url.params["cif"] == "12345678" and seen[0].url.params["zile"] == "60"
    )  # ANAF max
    assert seen[0].headers["authorization"] == "Bearer tok123"
    assert await spv.download("3001") == b"PK-zip-bytes"
    assert seen[1].url.params["id"] == "3001"


async def test_anaf_client_errors_are_readable() -> None:
    def no_messages(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"eroare": "Nu exista mesaje in ultimele 60 zile", "titlu": "Lista Mesaje"}
        )

    assert await _anaf(no_messages).list_messages("12345678", days=60) == []

    def no_rights(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"eroare": "Nu aveti drept in SPV pentru CIF=12345678"})

    with pytest.raises(SpvError, match="Nu aveti drept"):
        await _anaf(no_rights).list_messages("12345678", days=60)

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    with pytest.raises(SpvError, match="token"):
        await _anaf(unauthorized).download("1")
    with pytest.raises(SpvError, match="token"):
        AnafSpvClient("")


def test_unzip_ignores_the_signature() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("semnatura_9.xml", "<sig/>")
        zf.writestr("9.xml", "<Invoice/>")
    assert unzip_invoice(buf.getvalue()) == b"<Invoice/>"
    with pytest.raises(SpvError, match="zip"):
        unzip_invoice(b"not a zip")
    assert json  # (imported for fixtures above)


# -- sync: SPV → the client's bucket folder + documents table ------------------------------

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from langclaw.config.schema import BucketConfig, DocumentsConfig  # noqa: E402
from langclaw.documents import Bucket, DocumentServices, build_document_tools  # noqa: E402
from langclaw.documents.efactura.sync import sync_efactura  # noqa: E402
from langclaw.tenants import Tenant, tenant_scope  # noqa: E402
from tests.test_documents import FakeStore  # noqa: E402


class ScopedFakeStore(FakeStore):
    def for_schema(self, schema: str) -> FakeStore:
        return self.__dict__.setdefault(schema, ScopedFakeStore())


@pytest.fixture
def s3():
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="docs")
        yield client


def _services(s3, **efactura) -> DocumentServices:
    config = DocumentsConfig(efactura={"mode": "demo", **efactura})
    return DocumentServices(
        config,
        bucket=Bucket(BucketConfig(name="docs"), client=s3),
        store=ScopedFakeStore(),
        require_tenant=True,
    )


async def test_sync_files_each_invoice_once_under_the_client(s3) -> None:
    services = _services(s3)
    acme = services.scoped("acme")
    spv = DemoSpvClient(invoices=4)

    first = await sync_efactura(acme, spv, cif="RO12345678", days=60)
    assert len(first["filed"]) == 4 and first["skipped"] == 0 and first["errors"] == []
    again = await sync_efactura(acme, spv, cif="RO12345678", days=60)
    assert again["filed"] == [] and again["skipped"] == 4  # idempotent

    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="docs")["Contents"])
    assert len(keys) == 4 and all(k.startswith("tenants/acme/efactura/") for k in keys)
    rows = acme.store.rows
    received = [r for r in rows.values() if r["fields"]["direction"] == "in"]
    sent = [r for r in rows.values() if r["fields"]["direction"] == "out"]
    assert received and sent
    row = received[0]
    assert row["doc_type"] == "invoice" and row["status"] == "filed"
    assert row["receiver"] == "CLIENT 12345678 SRL" and row["sender"] != row["receiver"]
    assert row["fields"]["source"] == "efactura" and row["fields"]["supplier_cui"].startswith("RO")
    assert row["fields"]["vat_breakdown"] and row["fields"]["lines"]
    assert float(row["amount"]) > 0 and row["currency"] == "RON"


async def test_sync_keeps_clients_apart(s3) -> None:
    services = _services(s3)
    spv = DemoSpvClient(invoices=3)
    await sync_efactura(services.scoped("acme"), spv, cif="RO12345678", days=60)
    beta = services.scoped("beta")
    assert beta.store.rows == {}
    assert await beta.bucket.list("") == []


async def test_a_bad_invoice_is_filed_for_review_not_dropped(s3) -> None:
    class Broken(DemoSpvClient):
        async def download(self, message_id: str) -> bytes:
            archive = await super().download(message_id)
            xml = unzip_invoice(archive).replace(
                b"<cbc:TaxInclusiveAmount", b"<cbc:TaxInclusiveAmount x='1'", 1
            )
            xml = re.sub(rb"(<cbc:TaxInclusiveAmount[^>]*>)[^<]+", rb"\g<1>1.00", xml)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(f"{message_id}.xml", xml)
            return buf.getvalue()

    acme = _services(s3).scoped("acme")
    out = await sync_efactura(acme, Broken(invoices=1), cif="RO12345678", days=60)
    (key,) = out["filed"]
    row = acme.store.rows[key]
    assert row["status"] == "needs_review" and row["fields"]["problems"]


async def test_efactura_sync_tool_uses_the_clients_tax_id(s3) -> None:
    services = _services(s3)
    tools = {t.name: t for t in build_document_tools(services)}
    assert "error" in await tools["efactura_sync"].ainvoke({})  # no client
    with tenant_scope(Tenant(id="acme", name="ACME")):
        out = await tools["efactura_sync"].ainvoke({})
        assert "tax id" in out["error"]
    with tenant_scope(Tenant(id="acme", name="ACME", tax_id="RO12345678")):
        out = await tools["efactura_sync"].ainvoke({"days": 30})
    assert out["mode"] == "demo" and len(out["filed"]) == 6
    names = {t.name for t in build_document_tools(DocumentServices(DocumentsConfig()))}
    assert "efactura_sync" not in names  # off unless documents.efactura.mode is set


import re  # noqa: E402


def test_the_efactura_template_is_valid_against_the_real_tools(s3) -> None:
    import json as _json
    from pathlib import Path

    from langclaw.workflows.graph import parse_graph_spec

    names = {t.name for t in build_document_tools(_services(s3))}
    path = Path(__file__).resolve().parent.parent / "ui" / "templates" / "efactura_sync.graph.json"
    parse_graph_spec("efactura_sync", _json.loads(path.read_text()), available_tools=names)


import os  # noqa: E402

_PG = os.environ.get("LANGCLAW_TEST_POSTGRES_DSN", "")


@pytest.mark.skipif(not _PG, reason="set LANGCLAW_TEST_POSTGRES_DSN")
async def test_synced_invoices_land_in_the_clients_table_and_are_searchable(s3) -> None:
    from langclaw.documents import DocumentStore

    root = DocumentStore(_PG)
    pool = await root._db()
    await pool.execute("DROP SCHEMA IF EXISTS tenant_acme CASCADE")
    try:
        services = DocumentServices(
            DocumentsConfig(efactura={"mode": "demo"}),
            bucket=Bucket(BucketConfig(name="docs"), client=s3),
            store=root,
            require_tenant=True,
        )
        acme = services.scoped("acme")
        out = await sync_efactura(acme, DemoSpvClient(invoices=6), cif="RO12345678", days=60)
        assert len(out["filed"]) == 6 and out["errors"] == []
        incoming = await acme.store.search(fields={"direction": "in"}, limit=50)
        assert incoming and all(r["fields"]["source"] == "efactura" for r in incoming)
        invoices = await acme.store.search(doc_type="invoice", limit=50)
        assert invoices and all(r["amount"] > 0 for r in invoices)
        # nothing leaked into the shared (non-client) table
        assert not [r for r in await root.search(limit=200) if r["bucket_key"] in out["filed"]]
    finally:
        await pool.execute("DROP SCHEMA IF EXISTS tenant_acme CASCADE")
        await root.close()
