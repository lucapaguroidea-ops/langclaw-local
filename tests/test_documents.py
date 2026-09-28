"""Document tools: bucket (fake S3 via moto), text extraction, the documents table
(real Postgres when LANGCLAW_TEST_POSTGRES_DSN is set), and chat intake."""

from __future__ import annotations

import base64
import json
import os
from datetime import date, timedelta
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


async def _invoices(store: DocumentStore, n: int, *, sender: str = "ACME") -> None:
    pool = await store._db()
    await pool.executemany(
        "INSERT INTO documents (bucket_key, doc_type, sender, document_date, amount, currency) "
        "VALUES ($1, 'invoice', $2, $3, $4, 'RON')",
        [(f"inv/{i:04d}", sender, date(2024, 1, 1) + timedelta(days=i), 10) for i in range(n)],
    )


@needs_pg
async def test_search_pages_through_every_match_and_totals_count_them_all(
    store: DocumentStore,
) -> None:
    await _invoices(store, 250)
    await store.save("eur", {"doc_type": "invoice", "amount": 5, "currency": "EUR"})
    await store.save("lease", {"doc_type": "contract"})

    first = await store.search(doc_type="invoice", limit=200)
    rest = await store.search(doc_type="invoice", limit=200, offset=200)
    assert len(first) == 200 and len(rest) == 51
    assert {r["bucket_key"] for r in first} | {r["bucket_key"] for r in rest} == {
        *(f"inv/{i:04d}" for i in range(250)),
        "eur",
    }
    assert await store.totals(doc_type="invoice") == {
        "total": 251,
        "amounts": {"RON": 2500.0, "EUR": 5.0},
    }
    assert await store.totals(sender="nobody") == {"total": 0, "amounts": {}}


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


@needs_pg
async def test_chat_search_says_how_many_match_and_pages_to_the_rest(
    bucket: Bucket, store: DocumentStore
) -> None:
    await _invoices(store, 250)
    search = _tools(bucket, store)["documents_search"]

    first = await search.ainvoke({"doc_type": "invoice"})
    assert first["count"] == 20 and first["total"] == 250
    assert first["amounts"] == {"RON": 2500.0} and first["next_offset"] == 20
    assert "250" in first["note"] and "offset=20" in first["note"]

    seen = [d["bucket_key"] for d in first["documents"]]
    offset = first["next_offset"]
    while offset is not None:
        page = await search.ainvoke({"doc_type": "invoice", "limit": 200, "offset": offset})
        seen += [d["bucket_key"] for d in page["documents"]]
        offset = page["next_offset"]
    assert sorted(seen) == [f"inv/{i:04d}" for i in range(250)]
    assert "note" not in page and page["total"] == 250


@needs_pg
async def test_documents_api_pages_with_a_total(store: DocumentStore) -> None:
    await _invoices(store, 120)
    plane = _plane(DocumentServices(DocumentsConfig(), store=store))

    first = await plane.list_documents(doc_type="invoice")
    assert (first["count"], first["total"], first["offset"], first["next_offset"]) == (
        50,
        120,
        0,
        50,
    )
    assert first["amounts"] == {"RON": 1200.0}
    last = await plane.list_documents(doc_type="invoice", limit=100, offset=100)
    assert (last["count"], last["next_offset"]) == (20, None)
    assert last["documents"][-1]["bucket_key"] == "inv/0000"


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
    store = FakeStore()
    services = DocumentServices(config.documents, bucket=bucket, store=store)
    monkeypatch.setattr(doc_tools, "shared_services", lambda _cfg, **_kw: services)

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
    # marked 'processing' at once, so a bucket scan won't queue it again
    assert store.rows[wf_input["key"]]["status"] == "processing"


async def test_intake_off_leaves_attachments_to_the_agent(bucket: Bucket) -> None:
    from langclaw.documents.intake import intake_files

    assert intake_files([_attachment("a.pdf", b"x", AttachmentType.IMAGE)]) == []
    cfg = LangclawConfig()
    assert cfg.documents.intake_workflow == ""  # off by default


# -- intake workflow + bucket scan (slice 7) ---------------------------------------------


class FakeStore:
    """In-memory stand-in for DocumentStore (same upsert semantics)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def save(self, bucket_key: str, values: dict) -> dict:
        row = self.rows.setdefault(bucket_key, {"bucket_key": bucket_key, "fields": {}})
        values = {k: v for k, v in values.items() if v is not None}
        row["fields"] = {**row["fields"], **values.pop("fields", {})}
        row.update(values)
        return dict(row)

    async def get(self, bucket_key: str) -> dict | None:
        return self.rows.get(bucket_key)

    async def known_keys(self, keys: list[str]) -> set[str]:
        return {k for k in keys if k in self.rows}


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, msg) -> None:
        self.published.append(msg)


def _template(name: str) -> dict:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "ui" / "templates" / f"{name}.graph.json"
    return json.loads(path.read_text())


def test_templates_are_valid_against_the_real_document_tools(bucket: Bucket) -> None:
    import sys
    from pathlib import Path

    from langclaw.workflows.graph import parse_graph_spec

    services = DocumentServices(DocumentsConfig(), bucket=bucket, store=FakeStore())
    names = {t.name for t in build_document_tools(services, bus=_RecordingBus())}
    for name in ("document_intake", "bucket_scan"):
        parse_graph_spec(name, _template(name), available_tools=names)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
    import editor

    assert any(t.get("nodes", {}).get("classify") for t in editor.TEMPLATES.values())


async def test_start_intake_queues_each_new_file_once(bucket: Bucket) -> None:
    await bucket.put("inbox/a.pdf", b"a")
    await bucket.put("inbox/b.pdf", b"b")
    await bucket.put("other/c.pdf", b"c")
    store, bus = FakeStore(), _RecordingBus()
    await store.save("inbox/b.pdf", {"status": "filed"})
    services = DocumentServices(
        DocumentsConfig(intake_workflow="document_intake"), bucket=bucket, store=store
    )
    tools = {
        t.name: t
        for t in build_document_tools(
            services, bus=bus, report_to={"channel": "telegram", "chat_id": "99"}
        )
    }
    first = await tools["documents_start_intake"].ainvoke({})
    assert first == {"started": ["inbox/a.pdf"], "workflow": "document_intake"}
    (run,) = bus.published
    assert (run.origin, run.channel, run.chat_id) == ("workflow", "telegram", "99")
    assert run.metadata["trigger"] == "scan"
    assert json.loads(run.metadata["workflow_input"])["key"] == "inbox/a.pdf"
    assert store.rows["inbox/a.pdf"]["status"] == "processing"
    again = await tools["documents_start_intake"].ainvoke({})
    assert again["started"] == [] and len(bus.published) == 1


async def test_start_intake_needs_a_workflow_and_a_chat(bucket: Bucket) -> None:
    services = DocumentServices(DocumentsConfig(), bucket=bucket, store=FakeStore())
    assert "documents_start_intake" not in {t.name for t in build_document_tools(services)}
    tool = next(
        t
        for t in build_document_tools(services, bus=_RecordingBus())
        if t.name == "documents_start_intake"
    )
    assert "intake_workflow" in (await tool.ainvoke({}))["error"]
    services.config.intake_workflow = "document_intake"
    assert "review_chat_id" in (await tool.ainvoke({}))["error"]


@pytest.mark.parametrize(
    ("confidence", "answer", "status"),
    [(0.95, None, "filed"), (0.4, "approve", "filed"), (0.4, "reject", "rejected")],
)
async def test_document_intake_runs_on_the_real_tools(
    bucket: Bucket, confidence: float, answer: str | None, status: str
) -> None:
    from langclaw.workflows.executor import build_toolset_executor
    from langclaw.workflows.graph import GraphWorkflowRunner, build_state_graph, parse_graph_spec
    from langclaw.workflows.registry import WorkflowSpec

    await bucket.put(
        "inbox/inv.pdf", make_pdf("Invoice from ACME to Globex"), content_type="application/pdf"
    )
    store = FakeStore()
    services = DocumentServices(DocumentsConfig(), bucket=bucket, store=store)
    real = build_toolset_executor(build_document_tools(services))
    prompts: list[str] = []

    async def executor(request):
        if request.kind == "llm":  # the only fake: the model
            prompts.append(request.payload["prompt"])
            return request.schema(
                doc_type="invoice",
                sender="ACME",
                receiver="Globex",
                document_date="2026-09-01",
                amount=120.5,
                currency="EUR",
                summary="An invoice.",
                confidence=confidence,
                details={"iban": "IT60X0542811101000000123456", "due_date": "2026-10-01"},
            )
        return await real(request)

    parsed = parse_graph_spec("document_intake", _template("document_intake"))
    spec = WorkflowSpec(name="document_intake", graph=build_state_graph(parsed), graph_spec=parsed)
    runner = GraphWorkflowRunner(executor_provider=lambda: executor)
    wf_input = {"key": "inbox/inv.pdf", "filename": "inv.pdf", "mime_type": "", "caption": ""}
    result = await runner.start(spec, wf_input, run_id="document_intake:1")
    assert "Invoice from ACME to Globex" in prompts[0]
    if answer:
        assert result.status == "waiting" and "inbox/inv.pdf" not in store.rows
        result = await runner.resume(
            spec, "document_intake:1", {"action": answer, "by": "luca", "via": "ui"}
        )
    row = store.rows["inbox/inv.pdf"]
    assert row["status"] == status
    if status == "filed":
        assert result.status == "completed"
        assert (row["sender"], row["document_date"], row["amount"]) == (
            "ACME",
            "2026-09-01",
            120.5,
        )
        assert row["fields"] == {
            "iban": "IT60X0542811101000000123456",
            "due_date": "2026-10-01",
        }


# -- OCR (vision model) ------------------------------------------------------------------


class FakeVision:
    """A chat model stand-in: records each call and 'reads' a fixed text."""

    def __init__(self, text: str = "INVOICE 42 from ACME") -> None:
        self.text = text
        self.calls: list = []

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        self.calls.append(messages)
        return AIMessage(content=self.text)


def _images_in(call) -> list[str]:
    (msg,) = call
    return [b["image_url"]["url"] for b in msg.content if b.get("type") == "image_url"]


async def test_ocr_reads_a_scanned_pdf_page_by_page() -> None:
    from langclaw.documents.ocr import VisionOcr

    vision = FakeVision()
    ocr = VisionOcr(vision, max_pages=5)
    text, note = await ocr.read(make_pdf(""), content_type="application/pdf", filename="scan.pdf")
    assert text == "[page 1]\nINVOICE 42 from ACME" and note == ""
    (url,) = _images_in(vision.calls[0])
    assert url.startswith("data:image/png;base64,")


async def test_ocr_reads_a_photo_directly() -> None:
    from langclaw.documents.ocr import VisionOcr

    vision = FakeVision("Receipt, total 12.50 EUR")
    text, _ = await VisionOcr(vision).read(b"\xff\xd8jpeg", content_type="image/jpeg")
    assert text == "Receipt, total 12.50 EUR"
    assert _images_in(vision.calls[0]) == [
        "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode()
    ]


async def test_ocr_failure_is_a_note_not_a_crash() -> None:
    from langclaw.documents.ocr import VisionOcr

    class Down(FakeVision):
        async def ainvoke(self, messages):
            raise RuntimeError("model unavailable")

    text, note = await VisionOcr(Down()).read(b"img", content_type="image/png")
    assert text == "" and "model unavailable" in note


async def test_bucket_read_falls_back_to_ocr_only_when_there_is_no_text(bucket: Bucket) -> None:
    from langclaw.documents.ocr import VisionOcr

    await bucket.put("inbox/scan.pdf", make_pdf(""), content_type="application/pdf")
    await bucket.put("inbox/typed.pdf", make_pdf("Typed text"), content_type="application/pdf")
    await bucket.put("inbox/photo.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")
    vision = FakeVision()
    services = DocumentServices(DocumentsConfig(), bucket=bucket, ocr=VisionOcr(vision))
    read = {t.name: t for t in build_document_tools(services)}["bucket_read"]

    typed = await read.ainvoke({"key": "inbox/typed.pdf"})
    assert "Typed text" in typed["text"] and not typed.get("ocr") and vision.calls == []
    scan = await read.ainvoke({"key": "inbox/scan.pdf"})
    assert scan["ocr"] is True and "INVOICE 42" in scan["text"]
    photo = await read.ainvoke({"key": "inbox/photo.jpg"})
    assert photo["ocr"] is True and photo["text"] == "INVOICE 42 from ACME"


async def test_without_an_ocr_model_scans_keep_a_clear_note(bucket: Bucket) -> None:
    await bucket.put("inbox/scan.pdf", make_pdf(""), content_type="application/pdf")
    read = {t.name: t for t in _tools_list(bucket)}["bucket_read"]
    out = await read.ainvoke({"key": "inbox/scan.pdf"})
    assert out["text"] == "" and "LANGCLAW__DOCUMENTS__OCR_MODEL" in out["note"]


def _tools_list(bucket: Bucket) -> list:
    return build_document_tools(DocumentServices(DocumentsConfig(), bucket=bucket))


def test_ocr_is_built_from_config_only_when_a_model_is_set(monkeypatch) -> None:
    from langclaw.documents import ocr as ocr_mod

    assert DocumentServices(DocumentsConfig()).ocr is None
    monkeypatch.setattr(ocr_mod, "init_chat_model", lambda spec: FakeVision(spec))
    services = DocumentServices(DocumentsConfig(ocr_model="openrouter:google/gemini-2.5-flash"))
    assert services.ocr.model.text == "openrouter:google/gemini-2.5-flash"


def test_photos_join_intake_only_when_ocr_can_read_them() -> None:
    from langclaw.documents.intake import intake_files

    photo = Attachment(type=AttachmentType.IMAGE, mime_type="image/jpeg", data="aGk=")
    pdf = _attachment("a.pdf", b"x")
    assert intake_files([photo, pdf]) == [pdf]
    assert intake_files([photo, pdf], images=True) == [photo, pdf]


async def test_a_photo_sent_in_chat_is_stored_with_a_readable_name(bucket: Bucket) -> None:
    from langclaw.documents.intake import store_attachments

    store = FakeStore()
    services = DocumentServices(
        DocumentsConfig(ocr_model="openrouter:x/vision"), bucket=bucket, store=store
    )
    photo = Attachment(type=AttachmentType.IMAGE, mime_type="image/jpeg", data="aGk=")
    (wf_input,) = await store_attachments(services, [photo], caption="receipt")
    assert wf_input["filename"] == "photo.jpg" and wf_input["key"].endswith("-photo.jpg")
    assert store.rows[wf_input["key"]]["status"] == "processing"


# -- semantic search (embeddings) --------------------------------------------------------

_VOCAB = ["invoice", "electricity", "power", "rent", "lease", "apartment", "flight", "ticket"]
_SYNONYMS = {"power": "electricity", "lease": "rent", "apartment": "rent", "ticket": "flight"}


class FakeEmbeddings:
    """Deterministic 'meaning' vectors: one dimension per concept (synonyms merge)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _vec(self, text: str) -> list[float]:
        words = [_SYNONYMS.get(w, w) for w in text.lower().replace(",", " ").split()]
        return [float(words.count(c)) + 0.01 for c in _VOCAB]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self._vec(t) for t in texts]

    async def aembed_query(self, text: str) -> list[float]:
        self.calls.append([text])
        return self._vec(text)


@needs_pg
async def test_saved_documents_are_found_by_meaning(store: DocumentStore) -> None:
    embed = FakeEmbeddings()
    services = DocumentServices(DocumentsConfig(), store=store, embeddings=embed)
    tools = {t.name: t for t in build_document_tools(services)}
    await tools["documents_save"].ainvoke(
        {"bucket_key": "a", "doc_type": "invoice", "summary": "electricity invoice for March"}
    )
    await tools["documents_save"].ainvoke(
        {"bucket_key": "b", "doc_type": "contract", "summary": "apartment lease renewal"}
    )
    await tools["documents_save"].ainvoke({"bucket_key": "c", "summary": "flight ticket to Rome"})

    found = await tools["documents_semantic_search"].ainvoke({"query": "rent", "limit": 2})
    assert [d["bucket_key"] for d in found["documents"]][0] == "b"
    assert found["documents"][0]["similarity"] > found["documents"][1]["similarity"]
    assert "embedding" not in found["documents"][0]  # vectors stay out of the model's context
    assert found["next_offset"] == 2
    more = await tools["documents_semantic_search"].ainvoke(
        {"query": "rent", "limit": 2, "offset": 2}
    )
    assert len(more["documents"]) == 1 and more["next_offset"] is None
    assert {d["bucket_key"] for d in found["documents"] + more["documents"]} == {"a", "b", "c"}
    power = await tools["documents_semantic_search"].ainvoke({"query": "power bill"})
    assert power["documents"][0]["bucket_key"] == "a"


@needs_pg
async def test_semantic_search_combines_with_filters(store: DocumentStore) -> None:
    services = DocumentServices(DocumentsConfig(), store=store, embeddings=FakeEmbeddings())
    tools = {t.name: t for t in build_document_tools(services)}
    await tools["documents_save"].ainvoke(
        {"bucket_key": "x", "summary": "rent March", "status": "filed"}
    )
    await tools["documents_save"].ainvoke(
        {"bucket_key": "y", "summary": "rent April", "status": "rejected"}
    )
    found = await tools["documents_semantic_search"].ainvoke({"query": "rent", "status": "filed"})
    assert [d["bucket_key"] for d in found["documents"]] == ["x"]


@needs_pg
async def test_a_failed_embedding_still_saves_the_record(store: DocumentStore) -> None:
    class Down(FakeEmbeddings):
        async def aembed_documents(self, texts):
            raise RuntimeError("embeddings offline")

    services = DocumentServices(DocumentsConfig(), store=store, embeddings=Down())
    save = {t.name: t for t in build_document_tools(services)}["documents_save"]
    out = await save.ainvoke({"bucket_key": "z", "summary": "rent"})
    assert out["saved"]["bucket_key"] == "z" and "embeddings offline" in out["note"]


@needs_pg
async def test_reindex_embeds_records_saved_before_search_was_on(store: DocumentStore) -> None:
    await store.save("old", {"summary": "electricity invoice"})
    embed = FakeEmbeddings()
    services = DocumentServices(DocumentsConfig(), store=store, embeddings=embed)
    tools = {t.name: t for t in build_document_tools(services)}
    assert await tools["documents_reindex"].ainvoke({}) == {"embedded": 1, "remaining": 0}
    found = await tools["documents_semantic_search"].ainvoke({"query": "power"})
    assert found["documents"][0]["bucket_key"] == "old"


def test_semantic_tools_exist_only_with_an_embedding_model(monkeypatch) -> None:
    from langclaw.documents import embeddings as emb_mod

    names = {t.name for t in build_document_tools(DocumentServices(DocumentsConfig()))}
    assert "documents_semantic_search" not in names and "documents_reindex" not in names
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = {}
    monkeypatch.setattr(emb_mod, "OpenAIEmbeddings", lambda **kw: seen.update(kw) or "E")
    services = DocumentServices(DocumentsConfig(embedding_model="openai/text-embedding-3-small"))
    assert services.embeddings == "E"
    assert seen["model"] == "openai/text-embedding-3-small"
    assert seen["base_url"] == "https://openrouter.ai/api/v1"
    assert seen["api_key"] == "sk-or-test"
    names = {t.name for t in build_document_tools(services)}
    assert {"documents_semantic_search", "documents_reindex"} <= names


# -- control plane (UI / HTTP API) -------------------------------------------------------


def _plane(services: DocumentServices | None, enabled: bool = True):
    from langclaw.gateway.control import ControlPlane

    config = LangclawConfig()
    config.documents.enabled = enabled
    plane = ControlPlane(config=config, bus=MagicMock(), channels=[], agent_names=[])
    if services is not None:
        plane.documents = services
    return plane


async def test_documents_api_is_a_clear_error_when_documents_are_off() -> None:
    from langclaw.gateway.control import FeatureDisabledError

    with pytest.raises(FeatureDisabledError, match="LANGCLAW__DOCUMENTS__ENABLED"):
        await _plane(None, enabled=False).list_documents()


@needs_pg
async def test_documents_api_lists_filters_and_ranks(store: DocumentStore) -> None:
    services = DocumentServices(DocumentsConfig(), store=store, embeddings=FakeEmbeddings())
    save = {t.name: t for t in build_document_tools(services)}["documents_save"]
    await save.ainvoke({"bucket_key": "a", "summary": "electricity invoice", "sender": "Enel"})
    await save.ainvoke({"bucket_key": "b", "summary": "apartment lease", "status": "rejected"})
    plane = _plane(services)

    listed = await plane.list_documents()
    assert {d["bucket_key"] for d in listed["documents"]} == {"a", "b"}
    assert listed["semantic"] is True and listed["mode"] == "filter"
    assert [
        d["bucket_key"] for d in (await plane.list_documents(status="rejected"))["documents"]
    ] == ["b"]
    assert [d["bucket_key"] for d in (await plane.list_documents(sender="enel"))["documents"]] == [
        "a"
    ]

    ranked = await plane.list_documents(q="power bill", semantic=True)
    assert ranked["mode"] == "semantic" and ranked["documents"][0]["bucket_key"] == "a"
    assert "similarity" in ranked["documents"][0]


@needs_pg
async def test_documents_api_semantic_without_embeddings_falls_back_to_text(
    store: DocumentStore,
) -> None:
    await store.save("a", {"summary": "electricity invoice"})
    plane = _plane(DocumentServices(DocumentsConfig(), store=store))
    out = await plane.list_documents(q="electricity", semantic=True)
    assert out["mode"] == "text" and out["semantic"] is False
    assert [d["bucket_key"] for d in out["documents"]] == ["a"]


@needs_pg
async def test_documents_api_detail_has_a_download_link(
    bucket: Bucket, store: DocumentStore
) -> None:
    from langclaw.gateway.control import NotFoundError

    await bucket.put("inbox/inv.pdf", b"%PDF-1.4", content_type="application/pdf")
    await store.save("inbox/inv.pdf", {"summary": "invoice"})
    plane = _plane(DocumentServices(DocumentsConfig(), bucket=bucket, store=store))
    doc = await plane.get_document("inbox/inv.pdf")
    assert doc["document"]["summary"] == "invoice" and doc["link"].startswith("https://")
    with pytest.raises(NotFoundError):
        await plane.get_document("nope")


# -- type-specific fields -----------------------------------------------------------------


@needs_pg
async def test_search_filters_on_any_extracted_field(store: DocumentStore) -> None:
    services = DocumentServices(DocumentsConfig(), store=store)
    tools = {t.name: t for t in build_document_tools(services)}
    await tools["documents_save"].ainvoke(
        {
            "bucket_key": "c1",
            "doc_type": "contract",
            "fields": {"jurisdiction": "State of Delaware", "notice": {"days": 90}},
        }
    )
    await tools["documents_save"].ainvoke(
        {"bucket_key": "c2", "doc_type": "contract", "fields": {"jurisdiction": "Italy"}}
    )
    await tools["documents_save"].ainvoke(
        {"bucket_key": "i1", "doc_type": "invoice", "fields": {"tax_id": "IT0123"}}
    )
    search = tools["documents_search"]

    found = await search.ainvoke({"fields": {"jurisdiction": "delaware"}})
    assert [d["bucket_key"] for d in found["documents"]] == ["c1"]
    nested = await search.ainvoke({"fields": {"notice.days": "90"}})
    assert [d["bucket_key"] for d in nested["documents"]] == ["c1"]
    both = await search.ainvoke({"doc_type": "invoice", "fields": {"tax_id": "IT0123"}})
    assert [d["bucket_key"] for d in both["documents"]] == ["i1"]
    none = await search.ainvoke({"fields": {"jurisdiction": "delaware", "tax_id": "IT0123"}})
    assert none["documents"] == []


@needs_pg
async def test_field_filters_apply_to_semantic_search_and_the_api(store: DocumentStore) -> None:
    services = DocumentServices(DocumentsConfig(), store=store, embeddings=FakeEmbeddings())
    save = {t.name: t for t in build_document_tools(services)}["documents_save"]
    await save.ainvoke({"bucket_key": "r1", "summary": "rent", "fields": {"city": "Milan"}})
    await save.ainvoke({"bucket_key": "r2", "summary": "rent", "fields": {"city": "Rome"}})
    tools = {t.name: t for t in build_document_tools(services)}
    ranked = await tools["documents_semantic_search"].ainvoke(
        {"query": "lease", "fields": {"city": "rome"}}
    )
    assert [d["bucket_key"] for d in ranked["documents"]] == ["r2"]
    out = await _plane(services).list_documents(fields={"city": "milan"})
    assert [d["bucket_key"] for d in out["documents"]] == ["r1"]


def test_intake_template_keeps_type_specific_details() -> None:
    graph = _template("document_intake")
    details = graph["nodes"]["classify"]["output"]["details"]
    assert details["type"] == "object"
    assert details.get("required") is False  # a model that finds no extras still files
    assert graph["nodes"]["save"]["args"]["fields"] == "{{classify.details}}"


# -- tenants: one client's documents are invisible to another ----------------------------


async def test_scoped_buckets_keep_clients_apart(bucket: Bucket, s3) -> None:
    acme, beta = bucket.with_prefix("tenants/acme/"), bucket.with_prefix("tenants/beta/")
    await acme.put("inbox/a.pdf", b"acme", content_type="application/pdf")

    assert [o.key for o in await acme.list("inbox/")] == ["inbox/a.pdf"]  # relative keys
    assert await beta.list("") == []
    with pytest.raises(BucketError, match="no such object"):
        await beta.get("inbox/a.pdf")
    stored = [o["Key"] for o in s3.list_objects_v2(Bucket="docs")["Contents"]]
    assert stored == ["tenants/acme/inbox/a.pdf"]
    for crafted in ["../acme/inbox/a.pdf", "/tenants/acme/inbox/a.pdf", "inbox/../../acme/x", ""]:
        with pytest.raises(BucketError, match="Invalid file key"):
            await beta.get(crafted)
    assert (await acme.link("inbox/a.pdf")).startswith("https://")


@needs_pg
async def test_scoped_stores_keep_clients_apart(store: DocumentStore) -> None:
    acme, beta = store.for_schema("tenant_acme"), store.for_schema("tenant_beta")
    try:
        await acme.save("inbox/k.pdf", {"summary": "acme invoice", "sender": "Enel"})
        await beta.save("inbox/k.pdf", {"summary": "beta contract"})  # same key, own row
        assert (await acme.get("inbox/k.pdf"))["summary"] == "acme invoice"
        assert (await beta.get("inbox/k.pdf"))["summary"] == "beta contract"
        assert [d["summary"] for d in await beta.search(sender="Enel")] == []
        assert await store.get("inbox/k.pdf") is None  # the unscoped table is separate too
        assert acme.pool_key == beta.pool_key == store.pool_key  # one pool for all clients
    finally:
        pool = await store._db()
        await pool.execute("DROP SCHEMA IF EXISTS tenant_acme CASCADE")
        await pool.execute("DROP SCHEMA IF EXISTS tenant_beta CASCADE")


async def test_document_tools_refuse_without_a_client(bucket: Bucket) -> None:
    services = DocumentServices(
        DocumentsConfig(), bucket=bucket, store=FakeStore(), require_tenant=True
    )
    tools = {t.name: t for t in build_document_tools(services)}
    for name, args in [
        ("bucket_list", {}),
        ("bucket_read", {"key": "inbox/a.pdf"}),
        ("documents_search", {}),
        ("documents_save", {"bucket_key": "inbox/a.pdf"}),
    ]:
        out = await tools[name].ainvoke(args)
        assert "isn't linked to a client" in out["error"], name


async def test_document_tools_follow_the_current_client(bucket: Bucket) -> None:
    from langclaw.tenants import Tenant, tenant_scope

    stores: dict[str, FakeStore] = {}

    class ScopedFakeStore(FakeStore):
        def for_schema(self, schema: str) -> FakeStore:
            return stores.setdefault(schema, ScopedFakeStore())

    services = DocumentServices(
        DocumentsConfig(), bucket=bucket, store=ScopedFakeStore(), require_tenant=True
    )
    tools = {t.name: t for t in build_document_tools(services)}
    acme, beta = Tenant(id="acme", name="ACME"), Tenant(id="beta", name="Beta")

    with tenant_scope(acme):
        await bucket.with_prefix("tenants/acme/").put("inbox/a.txt", b"acme secret")
        await tools["documents_save"].ainvoke({"bucket_key": "inbox/a.txt", "summary": "x"})
        assert (await tools["bucket_read"].ainvoke({"key": "inbox/a.txt"}))["text"] == "acme secret"
    with tenant_scope(beta):
        assert (await tools["bucket_list"].ainvoke({}))["files"] == []
        assert "error" in await tools["bucket_read"].ainvoke({"key": "inbox/a.txt"})
        assert "error" in await tools["bucket_read"].ainvoke({"key": "../acme/inbox/a.txt"})
    assert set(stores) == {"tenant_acme"} and "inbox/a.txt" in stores["tenant_acme"].rows


async def test_a_scan_stays_inside_the_current_client(bucket: Bucket) -> None:
    from langclaw.tenants import Tenant, tenant_scope

    class ScopedFakeStore(FakeStore):
        def for_schema(self, schema: str) -> FakeStore:
            return self.__dict__.setdefault(schema, FakeStore())

    await bucket.with_prefix("tenants/acme/").put("inbox/a.pdf", b"a")
    await bucket.with_prefix("tenants/beta/").put("inbox/b.pdf", b"b")
    services = DocumentServices(
        DocumentsConfig(intake_workflow="document_intake"),
        bucket=bucket,
        store=ScopedFakeStore(),
        require_tenant=True,
    )
    bus = _RecordingBus()
    tools = {
        t.name: t
        for t in build_document_tools(
            services, bus=bus, report_to={"channel": "telegram", "chat_id": "firm"}
        )
    }
    acme = Tenant(id="acme", name="ACME", review_chat="telegram:-100acme")
    with tenant_scope(acme):
        out = await tools["documents_start_intake"].ainvoke({})
    assert out["started"] == ["inbox/a.pdf"]  # never beta's file
    (run,) = bus.published
    assert run.metadata["tenant"] == "acme" and run.chat_id == "-100acme"
    assert (
        "isn't linked to a client" in (await tools["documents_start_intake"].ainvoke({}))["error"]
    )
