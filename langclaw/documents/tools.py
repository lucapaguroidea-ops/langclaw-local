"""
Document tools for the agent and workflow steps.

- ``bucket_list`` / ``bucket_read`` / ``bucket_link`` / ``bucket_new_files``
- ``documents_save`` / ``documents_search`` / ``documents_get``
- ``documents_semantic_search`` / ``documents_reindex`` — with ``embedding_model``
- ``documents_start_intake`` — queue the intake workflow for new bucket files
  (needs the message bus; scheduled scans use it via a cron'd workflow)

Built by :func:`build_document_tools` when ``documents.enabled``; they share one
:class:`DocumentServices` (a bucket client and a pooled database connection).
Failures come back as ``{"error": ...}`` — never raised into the agent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langclaw.documents.bucket import Bucket, BucketError
from langclaw.documents.store import DocumentStore, DocumentStoreError
from langclaw.documents.text import extract_text
from langclaw.naming import check_tenant_id, tenant_bucket_prefix, tenant_schema
from langclaw.tenants import current_tenant

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from langclaw.bus.base import BaseMessageBus
    from langclaw.config.schema import DocumentsConfig
    from langclaw.documents.ocr import VisionOcr


class NoTenantError(ValueError):
    """Tenancy is on and this chat / run belongs to no client."""


_ERRORS = (BucketError, DocumentStoreError, ValueError)

NO_TENANT = (
    "This chat isn't linked to a client, so documents are unavailable here. "
    "Link it to a client on the console's Clients page."
)


class DocumentServices:
    """Lazily-created bucket client and documents store, shared by the tools.

    With ``require_tenant`` (tenancy on), tools go through :meth:`current`: the
    view of the current client's own bucket prefix and database schema, or a
    :class:`NoTenantError` when there is no client — they never see the whole
    bucket or another client's table.
    """

    def __init__(
        self,
        config: DocumentsConfig,
        *,
        bucket: Bucket | None = None,
        store: DocumentStore | None = None,
        ocr: VisionOcr | None = None,
        embeddings: Any | None = None,
        require_tenant: bool = False,
    ) -> None:
        self.config = config
        self._bucket = bucket
        self._store = store
        self._ocr = ocr
        self._embeddings = embeddings
        self.require_tenant = require_tenant
        self.tenant_id: str | None = None
        self._parent: DocumentServices | None = None
        self._scoped: dict[str, DocumentServices] = {}

    def scoped(self, tenant_id: str) -> DocumentServices:
        """This client's view: its bucket prefix and database schema (cached)."""
        child = self._scoped.get(tenant_id)
        if child is None:
            child = DocumentServices(self.config)
            child.tenant_id, child._parent = check_tenant_id(tenant_id), self
            self._scoped[tenant_id] = child
        return child

    def current(self) -> DocumentServices:
        """The services the current call may use.

        Raises:
            NoTenantError: tenancy is on and there is no current client.
        """
        if not self.require_tenant or self._parent is not None:
            return self
        tenant = current_tenant()
        if tenant is None:
            raise NoTenantError(NO_TENANT)
        return self.scoped(tenant.id)

    @property
    def bucket(self) -> Bucket:
        if self._bucket is None:
            if self._parent is not None:
                prefix = tenant_bucket_prefix(self.tenant_id)
                self._bucket = self._parent.bucket.with_prefix(prefix)
            else:
                self._bucket = Bucket(self.config.bucket)
        return self._bucket

    @property
    def ocr(self) -> VisionOcr | None:
        """The OCR reader, or ``None`` when no ``ocr_model`` is configured."""
        if self._parent is not None:
            return self._parent.ocr
        if self._ocr is None and self.config.ocr_model:
            from langclaw.documents.ocr import VisionOcr

            self._ocr = VisionOcr.from_spec(
                self.config.ocr_model, max_pages=self.config.ocr_max_pages
            )
        return self._ocr

    @property
    def embeddings(self) -> Any | None:
        """The embeddings client, or ``None`` when no ``embedding_model`` is set."""
        if self._parent is not None:
            return self._parent.embeddings
        if self._embeddings is None and self.config.embedding_model:
            from langclaw.documents.embeddings import build_embeddings

            self._embeddings = build_embeddings(self.config)
        return self._embeddings

    async def embed_record(self, record: dict[str, Any]) -> str:
        """Embed a saved record; returns a note on failure (``""`` on success)."""
        from langclaw.documents.embeddings import document_text

        try:
            (vector,) = await self.embeddings.aembed_documents([document_text(record)])
            await self.store.set_embedding(record["bucket_key"], vector)
        except Exception as exc:  # noqa: BLE001 — the record is saved; search lags
            return f"Saved, but not indexed for semantic search: {exc}"
        return ""

    @property
    def store(self) -> DocumentStore:
        if self._store is None:
            if self._parent is not None:
                self._store = self._parent.store.for_schema(tenant_schema(self.tenant_id))
            else:
                self._store = DocumentStore(self.config.database_url)
        return self._store


_SHARED: dict[tuple[Any, ...], DocumentServices] = {}


def shared_services(config: DocumentsConfig, *, require_tenant: bool = False) -> DocumentServices:
    """One :class:`DocumentServices` per bucket + database, reused across agent
    rebuilds (so the database pool isn't re-opened every rebuild)."""
    b = config.bucket
    key = (b.endpoint, b.name, b.access_key, config.database_url, require_tenant)
    services = _SHARED.get(key)
    if services is None:
        services = _SHARED[key] = DocumentServices(config, require_tenant=require_tenant)
    return services


def build_document_tools(
    services: DocumentServices,
    *,
    bus: BaseMessageBus | None = None,
    report_to: dict[str, str] | None = None,
) -> list[BaseTool]:
    """The document tools, closed over *services*.

    Args:
        services: Shared bucket + store.
        bus: The running message bus. ``documents_start_intake`` is only built
            when a bus is given (it starts runs by publishing to it).
        report_to: Default chat for intake runs started without a chat of
            their own (scheduled scans): ``{"channel", "chat_id"}``.
    """
    from langchain_core.tools import StructuredTool

    from langclaw.bus.base import InboundMessage

    cfg = services.config
    semantic = services.embeddings is not None

    async def bucket_list(prefix: str = "", limit: int = 50) -> dict:
        """List files in the document bucket, newest first.

        Args:
            prefix: Only keys starting with this (e.g. "inbox/").
            limit: Maximum number of files (1-500).
        """
        try:
            objects = await services.current().bucket.list(prefix, limit=max(1, min(limit, 500)))
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"files": [{"key": o.key, "size": o.size, "modified": o.modified} for o in objects]}

    async def bucket_read(key: str) -> dict:
        """Read a file from the document bucket and extract its text.

        PDFs and text files are read directly; scanned PDFs and photos go through
        OCR when an OCR model is configured ("ocr": true in the result).

        Args:
            key: The file's key in the bucket, e.g. "inbox/invoice-001.pdf".
        """
        try:
            data, content_type = await services.current().bucket.get(key)
        except _ERRORS as exc:
            return {"error": str(exc)}
        text, note = extract_text(data, content_type=content_type, filename=key)
        used_ocr = False
        if not text and services.ocr is not None:
            text, note = await services.ocr.read(data, content_type=content_type, filename=key)
            used_ocr = bool(text)
        truncated = len(text) > cfg.max_text_chars
        out = {
            "key": key,
            "content_type": content_type,
            "size": len(data),
            "text": text[: cfg.max_text_chars],
            "truncated": truncated,
        }
        if used_ocr:
            out["ocr"] = True
        if note:
            out["note"] = note
        return out

    async def bucket_link(key: str, expires_minutes: int = 60) -> dict:
        """Get a temporary download link for a file in the document bucket.

        Args:
            key: The file's key.
            expires_minutes: How long the link works (1-10080).
        """
        try:
            url = await services.current().bucket.link(
                key, expires_s=60 * max(1, min(expires_minutes, 10080))
            )
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"key": key, "url": url, "expires_minutes": expires_minutes}

    async def bucket_new_files(prefix: str = "", limit: int = 50) -> dict:
        """List files in the bucket that aren't in the documents table yet.

        Args:
            prefix: Only keys starting with this (default: the intake prefix).
            limit: Maximum number of new files to return.
        """
        try:
            objects = await services.current().bucket.list(prefix or cfg.intake_prefix, limit=1000)
            files = [o for o in objects if not o.key.endswith("/")]
            known = await services.current().store.known_keys([o.key for o in files])
        except _ERRORS as exc:
            return {"error": str(exc)}
        new = [o.key for o in files if o.key not in known][: max(1, min(limit, 500))]
        return {"new_files": new}

    async def documents_save(
        bucket_key: str,
        sender: str = "",
        receiver: str = "",
        document_date: str = "",
        doc_type: str = "",
        amount: float | None = None,
        currency: str = "",
        summary: str = "",
        filename: str = "",
        status: str = "filed",
        fields: dict[str, Any] | None = None,
    ) -> dict:
        """Save (or update) the record for a document in the bucket.

        Saving the same bucket_key again updates that record — never a duplicate.

        Args:
            bucket_key: The document's key in the bucket.
            sender: Who sent / issued it.
            receiver: Who it's addressed to.
            document_date: The document's own date, YYYY-MM-DD.
            doc_type: e.g. invoice, contract, letter, receipt.
            amount: Total amount, if any.
            currency: e.g. EUR.
            summary: One or two sentences on what it is.
            filename: Original file name.
            status: e.g. filed, needs_review.
            fields: Type-specific facts (tax_id, jurisdiction, due_date, ...);
                merged into the record and searchable with documents_search(fields=...).
        """
        values = {
            "sender": sender,
            "receiver": receiver,
            "document_date": document_date or None,
            "doc_type": doc_type,
            "amount": amount,
            "currency": currency,
            "summary": summary,
            "filename": filename or bucket_key.rsplit("/", 1)[-1],
            "status": status,
            "fields": fields or {},
        }
        try:
            saved = await services.current().store.save(bucket_key, values)
        except _ERRORS as exc:
            return {"error": str(exc)}
        out: dict[str, Any] = {"saved": saved}
        if semantic:
            note = await services.current().embed_record(saved)
            if note:
                out["note"] = note
        return out

    async def documents_search(
        text: str = "",
        sender: str = "",
        receiver: str = "",
        doc_type: str = "",
        date_from: str = "",
        date_to: str = "",
        status: str = "",
        fields: dict[str, str] | None = None,
        limit: int = 20,
    ) -> dict:
        """Search filed documents. All filters are optional and combined.

        Args:
            text: Words in the summary, file name, or extracted fields.
            sender: Part of the sender's name.
            receiver: Part of the receiver's name.
            doc_type: Exact type, e.g. invoice.
            date_from: Earliest document date, YYYY-MM-DD.
            date_to: Latest document date, YYYY-MM-DD.
            status: e.g. filed, needs_review.
            fields: Filters on type-specific extracted fields, field name to text,
                e.g. jurisdiction=Delaware or tax_id=IT0123 (substring match; a
                dotted name like notice.days reaches nested values).
            limit: Maximum results (1-200).
        """
        try:
            rows = await services.current().store.search(
                text=text,
                sender=sender,
                receiver=receiver,
                doc_type=doc_type,
                date_from=date_from,
                date_to=date_to,
                status=status,
                fields=fields,
                limit=limit,
            )
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"documents": rows, "count": len(rows)}

    async def documents_semantic_search(
        query: str,
        sender: str = "",
        receiver: str = "",
        doc_type: str = "",
        date_from: str = "",
        date_to: str = "",
        status: str = "",
        fields: dict[str, str] | None = None,
        limit: int = 10,
    ) -> dict:
        """Find filed documents by meaning, e.g. "power bills" finds electricity invoices.

        Results are ranked by similarity (0-1); the optional filters narrow them.

        Args:
            query: What you're looking for, in plain words.
            sender: Part of the sender's name.
            receiver: Part of the receiver's name.
            doc_type: Exact type, e.g. invoice.
            date_from: Earliest document date, YYYY-MM-DD.
            date_to: Latest document date, YYYY-MM-DD.
            status: e.g. filed, needs_review.
            fields: Filters on type-specific extracted fields, field name to text.
            limit: Maximum results (1-200).
        """
        try:
            vector = await services.embeddings.aembed_query(query)
            rows = await services.current().store.similar(
                vector,
                limit=limit,
                sender=sender,
                receiver=receiver,
                doc_type=doc_type,
                date_from=date_from,
                date_to=date_to,
                status=status,
                fields=fields,
            )
        except Exception as exc:  # noqa: BLE001 — embeddings or database, as text
            return {"error": str(exc)}
        return {"documents": rows, "count": len(rows)}

    async def documents_reindex(limit: int = 100) -> dict:
        """Index records saved before semantic search was turned on (or whose indexing failed).

        Args:
            limit: Records to index in this call (1-1000); repeat while "remaining" > 0.
        """
        try:
            rows = await services.current().store.without_embedding(max(1, min(limit, 1000)))
            done = 0
            for row in rows:
                if not await services.current().embed_record(row):
                    done += 1
            remaining = await services.current().store.count_without_embedding()
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"embedded": done, "remaining": remaining}

    async def documents_get(bucket_key: str) -> dict:
        """Get the filed record for one document.

        Args:
            bucket_key: The document's key in the bucket.
        """
        try:
            row = await services.current().store.get(bucket_key)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"document": row} if row else {"error": f"No record for {bucket_key!r}."}

    async def documents_start_intake(
        prefix: str = "", limit: int = 20, channel: str = "", chat_id: str = ""
    ) -> dict:
        """Start the intake workflow for every bucket file not filed yet.

        Each new file gets a 'processing' record first, so a second scan never
        queues it twice. Results and review requests go to the given chat, else
        the client's review chat, else the configured review chat.

        Args:
            prefix: Only keys starting with this (default: the intake prefix).
            limit: Maximum number of files to start (1-200).
            channel: Channel to report to (e.g. telegram); default: the review channel.
            chat_id: Chat to report to; default: the review chat.
        """
        if not cfg.intake_workflow:
            return {"error": "No intake workflow: set documents.intake_workflow."}
        tenant = current_tenant() if services.require_tenant else None
        default = (tenant.review_target() if tenant else None) or {
            "channel": (report_to or {}).get("channel", ""),
            "chat_id": (report_to or {}).get("chat_id", ""),
        }
        target_channel = channel or default["channel"]
        target_chat = chat_id or default["chat_id"]
        if not (target_channel and target_chat):
            return {
                "error": "No chat to report to: pass channel and chat_id, or set "
                "workflows.review_channel and workflows.review_chat_id."
            }
        try:
            objects = await services.current().bucket.list(prefix or cfg.intake_prefix, limit=1000)
            files = [o for o in objects if not o.key.endswith("/")]
            known = await services.current().store.known_keys([o.key for o in files])
            new = [o.key for o in files if o.key not in known][: max(1, min(limit, 200))]
            for key in new:
                await services.current().store.save(key, {"status": "processing"})
        except _ERRORS as exc:
            return {"error": str(exc)}
        for key in new:
            filename = key.rsplit("/", 1)[-1]
            await bus.publish(
                InboundMessage(
                    channel=target_channel,
                    user_id=target_chat,
                    context_id=target_chat,
                    chat_id=target_chat,
                    content=f"run workflow {cfg.intake_workflow}",
                    origin="workflow",
                    metadata={
                        "workflow_name": cfg.intake_workflow,
                        "workflow_input": json.dumps(
                            {"key": key, "filename": filename, "mime_type": "", "caption": ""}
                        ),
                        "trigger": "scan",
                        **({"tenant": tenant.id} if tenant else {}),
                    },
                )
            )
        return {"started": new, "workflow": cfg.intake_workflow}

    fns = [
        bucket_list,
        bucket_read,
        bucket_link,
        bucket_new_files,
        documents_save,
        documents_search,
        documents_get,
    ]
    if semantic:
        fns += [documents_semantic_search, documents_reindex]
    if bus is not None:
        fns.append(documents_start_intake)
    return [StructuredTool.from_function(coroutine=fn, parse_docstring=True) for fn in fns]
