"""
Document tools for the agent and workflow steps.

- ``bucket_list`` / ``bucket_read`` / ``bucket_link`` / ``bucket_new_files``
- ``documents_save`` / ``documents_search`` / ``documents_get``
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

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from langclaw.bus.base import BaseMessageBus
    from langclaw.config.schema import DocumentsConfig

_ERRORS = (BucketError, DocumentStoreError, ValueError)


class DocumentServices:
    """Lazily-created bucket client and documents store, shared by the tools."""

    def __init__(
        self,
        config: DocumentsConfig,
        *,
        bucket: Bucket | None = None,
        store: DocumentStore | None = None,
    ) -> None:
        self.config = config
        self._bucket = bucket
        self._store = store

    @property
    def bucket(self) -> Bucket:
        if self._bucket is None:
            self._bucket = Bucket(self.config.bucket)
        return self._bucket

    @property
    def store(self) -> DocumentStore:
        if self._store is None:
            self._store = DocumentStore(self.config.database_url)
        return self._store


_SHARED: dict[tuple[str, ...], DocumentServices] = {}


def shared_services(config: DocumentsConfig) -> DocumentServices:
    """One :class:`DocumentServices` per bucket + database, reused across agent
    rebuilds (so the database pool isn't re-opened every rebuild)."""
    b = config.bucket
    key = (b.endpoint, b.name, b.access_key, config.database_url)
    services = _SHARED.get(key)
    if services is None:
        services = _SHARED[key] = DocumentServices(config)
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

    async def bucket_list(prefix: str = "", limit: int = 50) -> dict:
        """List files in the document bucket, newest first.

        Args:
            prefix: Only keys starting with this (e.g. "inbox/").
            limit: Maximum number of files (1-500).
        """
        try:
            objects = await services.bucket.list(prefix, limit=max(1, min(limit, 500)))
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"files": [{"key": o.key, "size": o.size, "modified": o.modified} for o in objects]}

    async def bucket_read(key: str) -> dict:
        """Read a file from the document bucket and extract its text (PDFs and text files).

        Args:
            key: The file's key in the bucket, e.g. "inbox/invoice-001.pdf".
        """
        try:
            data, content_type = await services.bucket.get(key)
        except _ERRORS as exc:
            return {"error": str(exc)}
        text, note = extract_text(data, content_type=content_type, filename=key)
        truncated = len(text) > cfg.max_text_chars
        out = {
            "key": key,
            "content_type": content_type,
            "size": len(data),
            "text": text[: cfg.max_text_chars],
            "truncated": truncated,
        }
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
            url = await services.bucket.link(
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
            objects = await services.bucket.list(prefix or cfg.intake_prefix, limit=1000)
            files = [o for o in objects if not o.key.endswith("/")]
            known = await services.store.known_keys([o.key for o in files])
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
            fields: Anything else extracted (merged into the record).
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
            return {"saved": await services.store.save(bucket_key, values)}
        except _ERRORS as exc:
            return {"error": str(exc)}

    async def documents_search(
        text: str = "",
        sender: str = "",
        receiver: str = "",
        doc_type: str = "",
        date_from: str = "",
        date_to: str = "",
        status: str = "",
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
            limit: Maximum results (1-200).
        """
        try:
            rows = await services.store.search(
                text=text,
                sender=sender,
                receiver=receiver,
                doc_type=doc_type,
                date_from=date_from,
                date_to=date_to,
                status=status,
                limit=limit,
            )
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"documents": rows, "count": len(rows)}

    async def documents_get(bucket_key: str) -> dict:
        """Get the filed record for one document.

        Args:
            bucket_key: The document's key in the bucket.
        """
        try:
            row = await services.store.get(bucket_key)
        except _ERRORS as exc:
            return {"error": str(exc)}
        return {"document": row} if row else {"error": f"No record for {bucket_key!r}."}

    async def documents_start_intake(
        prefix: str = "", limit: int = 20, channel: str = "", chat_id: str = ""
    ) -> dict:
        """Start the intake workflow for every bucket file not filed yet.

        Each new file gets a 'processing' record first, so a second scan never
        queues it twice. Results and review requests go to the given chat, else
        to the configured review chat.

        Args:
            prefix: Only keys starting with this (default: the intake prefix).
            limit: Maximum number of files to start (1-200).
            channel: Channel to report to (e.g. telegram); default: the review channel.
            chat_id: Chat to report to; default: the review chat.
        """
        if not cfg.intake_workflow:
            return {"error": "No intake workflow: set documents.intake_workflow."}
        target_channel = channel or (report_to or {}).get("channel", "")
        target_chat = chat_id or (report_to or {}).get("chat_id", "")
        if not (target_channel and target_chat):
            return {
                "error": "No chat to report to: pass channel and chat_id, or set "
                "workflows.review_channel and workflows.review_chat_id."
            }
        try:
            objects = await services.bucket.list(prefix or cfg.intake_prefix, limit=1000)
            files = [o for o in objects if not o.key.endswith("/")]
            known = await services.store.known_keys([o.key for o in files])
            new = [o.key for o in files if o.key not in known][: max(1, min(limit, 200))]
            for key in new:
                await services.store.save(key, {"status": "processing"})
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
    if bus is not None:
        fns.append(documents_start_intake)
    return [StructuredTool.from_function(coroutine=fn, parse_docstring=True) for fn in fns]
