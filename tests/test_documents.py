"""Document tools: bucket (fake S3 via moto), text extraction, the documents table
(real Postgres when LANGCLAW_TEST_POSTGRES_DSN is set), and chat intake."""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import MagicMock

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from langclaw.bus.base import Attachment, AttachmentType  # noqa: E402
from langclaw.config.schema import BucketConfig, DocumentsConfig, LangclawConfig  # noqa: E402
from langclaw.documents import (  # noqa: E402
    Bucket,
    BucketError,
    DocumentServices,
    DocumentStore,
    DocumentStoreError,
    build_document_tools,
    extract_text,
)

PG_DSN = os.environ.get("LANGCLAW_TEST_POSTGRES_DSN", "")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set LANGCLAW_TEST_POSTGRES_DSN")


def make_pdf(text: str) -> bytes:
    """A minimal one-page PDF with a text layer (no PDF library needed)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


# -- config ---------------------------------------------------------------------------


def test_bucket_config_falls_back_to_railway_variables(monkeypatch) -> None:
    monkeypatch.setenv("BUCKET_NAME", "durable-taco")
    monkeypatch.setenv("BUCKET_ACCESS_KEY", "ak")
    monkeypatch.setenv("BUCKET_SECRET_KEY", "sk")
    monkeypatch.setenv("DOCUMENTS_DATABASE_URL", "postgresql://x")
    cfg = LangclawConfig().documents
    assert (cfg.bucket.name, cfg.bucket.configured) == ("durable-taco", True)
    assert cfg.database_url == "postgresql://x"
    explicit = BucketConfig(name="mine")
    assert explicit.name == "mine"


def test_missing_bucket_or_database_is_a_clear_error(monkeypatch) -> None:
    for var in ("BUCKET_NAME", "BUCKET_ACCESS_KEY", "BUCKET_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(BucketError, match="No bucket configured"):
        Bucket(BucketConfig())
    with pytest.raises(DocumentStoreError, match="No documents database"):
        DocumentStore("")


# -- text -------------------------------------------------------------------------------


def test_extract_text_from_pdf_and_text_files() -> None:
    text, note = extract_text(make_pdf("Invoice 42 from ACME"), filename="a.pdf")
    assert "Invoice 42 from ACME" in text and note == ""
    assert extract_text(b"hello", content_type="text/plain") == ("hello", "")
    text, note = extract_text(b"\x89PNG...", content_type="image/png")
    assert text == "" and "OCR" in note
    text, note = extract_text(b"%PDF-1.4 broken")
    assert text == "" and note


# -- bucket (moto) --------------------------------------------------------------------------


@pytest.fixture
def s3():
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="docs")
        yield client


@pytest.fixture
def bucket(s3) -> Bucket:
    return Bucket(BucketConfig(name="docs"), client=s3)


async def test_bucket_put_list_get_link(bucket: Bucket) -> None:
    await bucket.put("inbox/a.pdf", b"one", content_type="application/pdf")
    await bucket.put("inbox/b.txt", b"two", content_type="text/plain")
    listed = await bucket.list("inbox/")
    assert {o.key for o in listed} == {"inbox/a.pdf", "inbox/b.txt"}
    assert await bucket.get("inbox/b.txt") == (b"two", "text/plain")
    assert "inbox/a.pdf" in await bucket.link("inbox/a.pdf")


async def test_bucket_errors_are_readable(bucket: Bucket) -> None:
    with pytest.raises(BucketError, match="read 'nope' failed: no such object"):
        await bucket.get("nope")
    with pytest.raises(BucketError, match="link 'nope'"):
        await bucket.link("nope")


# -- store + tools (Postgres) ---------------------------------------------------------------


@pytest.fixture
async def store():
    s = DocumentStore(PG_DSN)
    pool = await s._db()
    await pool.execute("TRUNCATE documents")
    yield s
    await s.close()


def _tools(bucket: Bucket, store: DocumentStore | None, **cfg) -> dict:
    services = DocumentServices(DocumentsConfig(**cfg), bucket=bucket, store=store)
    return {t.name: t for t in build_document_tools(services)}


@needs_pg
async def test_save_is_an_upsert_and_extra_fields_merge(store: DocumentStore) -> None:
    first = await store.save(
        "inbox/a.pdf",
        {"sender": "ACME", "document_date": "2026-09-12", "amount": "1,200.50", "po": "7"},
    )
    assert first["sender"] == "ACME" and first["amount"] == 1200.5
    assert first["document_date"] == "2026-09-12" and first["fields"] == {"po": "7"}
    again = await store.save("inbox/a.pdf", {"receiver": "Paguroidea", "vat": "21%"})
    assert again["id"] == first["id"]
    assert (again["sender"], again["receiver"]) == ("ACME", "Paguroidea")
    assert again["fields"] == {"po": "7", "vat": "21%"}


@needs_pg
async def test_search_filters(store: DocumentStore) -> None:
    await store.save(
        "k1",
        {
            "sender": "ACME Corp",
            "doc_type": "invoice",
            "document_date": "2026-01-10",
            "summary": "Hosting",
        },
    )  # noqa: E501
    await store.save(
        "k2",
        {
            "sender": "Globex",
            "doc_type": "contract",
            "document_date": "2026-05-01",
            "summary": "Data privacy penalties",
        },
    )  # noqa: E501
    await store.save(
        "k3", {"sender": "ACME Corp", "doc_type": "invoice", "document_date": "2026-08-01"}
    )  # noqa: E501
    keys = lambda rows: [r["bucket_key"] for r in rows]  # noqa: E731
    assert keys(await store.search(sender="acme")) == ["k3", "k1"]
    assert keys(await store.search(text="privacy")) == ["k2"]
    assert keys(await store.search(doc_type="invoice", date_from="2026-06-01")) == ["k3"]
    assert keys(await store.search(date_to="2026-02-01")) == ["k1"]
    assert await store.known_keys(["k1", "zz"]) == {"k1"}


@needs_pg
async def test_bad_date_is_a_clear_error(store: DocumentStore) -> None:
    with pytest.raises(DocumentStoreError, match="YYYY-MM-DD"):
        await store.save("k", {"document_date": "12/09/2026"})


@needs_pg
async def test_tools_end_to_end(bucket: Bucket, store: DocumentStore) -> None:
    tools = _tools(bucket, store, intake_prefix="inbox/")
    await bucket.put("inbox/inv.pdf", make_pdf("Invoice from ACME"), content_type="application/pdf")
    await bucket.put("inbox/old.pdf", make_pdf("Old"), content_type="application/pdf")

    read = await tools["bucket_read"].ainvoke({"key": "inbox/inv.pdf"})
    assert "Invoice from ACME" in read["text"] and read["truncated"] is False

    await tools["documents_save"].ainvoke({"bucket_key": "inbox/old.pdf", "sender": "X"})
    new = await tools["bucket_new_files"].ainvoke({})
    assert new == {"new_files": ["inbox/inv.pdf"]}

    saved = await tools["documents_save"].ainvoke(
        {
            "bucket_key": "inbox/inv.pdf",
            "sender": "ACME",
            "document_date": "2026-09-12",
            "doc_type": "invoice",
            "fields": {"invoice_no": "42"},
        }
    )
    assert saved["saved"]["filename"] == "inv.pdf"
    found = await tools["documents_search"].ainvoke({"sender": "acme"})
    assert found["count"] == 1 and found["documents"][0]["fields"]["invoice_no"] == "42"
    assert (await tools["documents_get"].ainvoke({"bucket_key": "nope"}))["error"]
    bad = await tools["documents_save"].ainvoke({"bucket_key": "k", "document_date": "soon"})
    assert "YYYY-MM-DD" in bad["error"]


async def test_bucket_tools_report_errors_as_dicts(bucket: Bucket) -> None:
    tools = _tools(bucket, None)
    assert "no such object" in (await tools["bucket_read"].ainvoke({"key": "x"}))["error"]
    assert "no such object" in (await tools["bucket_link"].ainvoke({"key": "x"}))["error"]


async def test_bucket_read_truncates_long_text(bucket: Bucket) -> None:
    await bucket.put("big.txt", b"x" * 500, content_type="text/plain")
    out = await _tools(bucket, None, max_text_chars=100)["bucket_read"].ainvoke({"key": "big.txt"})
    assert len(out["text"]) == 100 and out["truncated"] is True


# -- chat intake ----------------------------------------------------------------------------


def _attachment(name: str, data: bytes, kind=AttachmentType.FILE) -> Attachment:
    return Attachment(
        type=kind,
        mime_type="application/pdf",
        filename=name,
        data=base64.b64encode(data).decode(),
    )


async def test_intake_stores_files_and_starts_the_workflow(bucket: Bucket, monkeypatch) -> None:
    from langclaw.bus.base import InboundMessage
    from langclaw.documents import tools as doc_tools
    from langclaw.gateway.manager import GatewayManager
    from tests.test_graph_workflow_gateway import _Bus, _FakeChannel

    config = LangclawConfig()
    config.documents.enabled = True
    config.documents.intake_workflow = "document_intake"
    services = DocumentServices(config.documents, bucket=bucket)
    monkeypatch.setattr(doc_tools, "shared_services", lambda _cfg: services)

    bus = _Bus()
    cp = MagicMock()
    cp.get.return_value = MagicMock()
    mgr = GatewayManager(
        config=config,
        bus=bus,
        checkpointer_backend=cp,
        agent=MagicMock(),
        channels=[_FakeChannel()],
    )
    msg = InboundMessage(
        channel="telegram",
        user_id="42",
        context_id="42",
        chat_id="42",
        content="September invoice",
        attachments=[
            _attachment("Invoice #7.pdf", make_pdf("x")),
            _attachment("pic.jpg", b"img", AttachmentType.IMAGE),
        ],
    )
    await mgr._handle(msg)

    (run,) = bus.published  # one file → one run; the photo isn't intake
    assert run.origin == "workflow" and run.metadata["workflow_name"] == "document_intake"
    wf_input = json.loads(run.metadata["workflow_input"])
    assert wf_input["key"].startswith("inbox/") and wf_input["key"].endswith("-Invoice_7.pdf")
    assert wf_input["caption"] == "September invoice"
    assert (await bucket.get(wf_input["key"]))[0].startswith(b"%PDF")
    sent = mgr._channel_map["telegram"].sent[-1].content
    assert sent == "📥 Saved Invoice #7.pdf — running document_intake."


async def test_intake_off_leaves_attachments_to_the_agent(bucket: Bucket) -> None:
    from langclaw.documents.intake import intake_files

    assert intake_files([_attachment("a.pdf", b"x", AttachmentType.IMAGE)]) == []
    cfg = LangclawConfig()
    assert cfg.documents.intake_workflow == ""  # off by default
