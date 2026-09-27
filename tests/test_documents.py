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
    store = FakeStore()
    services = DocumentServices(config.documents, bucket=bucket, store=store)
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
        assert row["fields"]["confidence"] == confidence


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
