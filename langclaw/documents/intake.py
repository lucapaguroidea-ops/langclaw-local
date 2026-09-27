"""
Chat intake — a document sent in chat goes to the bucket and into a workflow.

With ``documents.intake_workflow`` set, the gateway calls
:func:`store_attachments` for incoming messages: each *file* attachment (not
photos, audio, or video) is uploaded under ``documents.intake_prefix`` and gets
a run input for the intake workflow, ``{"key", "filename", "mime_type",
"caption"}``. The gateway then starts one workflow run per file — no agent turn.
"""

from __future__ import annotations

import base64
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from langclaw.bus.base import AttachmentType

if TYPE_CHECKING:
    from langclaw.bus.base import Attachment
    from langclaw.documents.tools import DocumentServices

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def intake_files(attachments: list[Attachment]) -> list[Attachment]:
    """The attachments intake handles: files with inline data."""
    return [a for a in attachments if a.type == AttachmentType.FILE and a.data]


def intake_key(prefix: str, filename: str) -> str:
    """``<prefix><YYYY-MM-DD>/<short id>-<safe filename>`` — unique and readable."""
    safe = _UNSAFE.sub("_", filename or "document").strip("._") or "document"
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"{prefix}{day}/{uuid.uuid4().hex[:8]}-{safe[:120]}"


async def store_attachments(
    services: DocumentServices, attachments: list[Attachment], *, caption: str = ""
) -> list[dict[str, Any]]:
    """Upload each intake file; return one workflow input per stored file.

    Raises:
        BucketError: an upload failed (the caller tells the user).
        DocumentStoreError: the documents table couldn't be written.
    """
    inputs = []
    for att in intake_files(attachments):
        key = intake_key(services.config.intake_prefix, att.filename)
        await services.bucket.put(key, base64.b64decode(att.data), content_type=att.mime_type)
        # A 'processing' row right away, so a bucket scan never queues it twice.
        await services.store.save(
            key, {"status": "processing", "filename": att.filename, "mime_type": att.mime_type}
        )
        inputs.append(
            {
                "key": key,
                "filename": att.filename,
                "mime_type": att.mime_type,
                "caption": caption,
            }
        )
    return inputs
