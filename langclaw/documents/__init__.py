"""
Documents — a bucket for files and a ``documents`` table for what was extracted.

Enable with ``LANGCLAW__DOCUMENTS__ENABLED=true`` (needs ``langclaw[documents]``).
The agent and workflow steps then get ``bucket_*`` and ``documents_*`` tools
(:mod:`langclaw.documents.tools`), and with ``documents.intake_workflow`` a file
sent in chat is saved to the bucket and handed to that workflow
(:mod:`langclaw.documents.intake`).
"""

from __future__ import annotations

from langclaw.documents.bucket import Bucket, BucketError, BucketObject
from langclaw.documents.store import DocumentStore, DocumentStoreError
from langclaw.documents.text import extract_text
from langclaw.documents.tools import DocumentServices, build_document_tools, shared_services

__all__ = [
    "Bucket",
    "BucketError",
    "BucketObject",
    "DocumentServices",
    "DocumentStore",
    "DocumentStoreError",
    "build_document_tools",
    "extract_text",
    "shared_services",
]
