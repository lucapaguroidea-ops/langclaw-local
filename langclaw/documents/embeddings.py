"""
Embeddings for semantic document search.

A record is embedded from what was extracted (type, sender, receiver, date,
summary, extra fields) when ``documents_save`` stores it; the vector sits in the
``documents.embedding`` column and ``documents_semantic_search`` ranks by cosine
similarity. Enable with ``LANGCLAW__DOCUMENTS__EMBEDDING_MODEL`` (e.g.
``openai/text-embedding-3-small``) — served through OpenRouter by default, or any
OpenAI-compatible endpoint via ``embedding_base_url``.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from langchain_openai import OpenAIEmbeddings

if TYPE_CHECKING:
    from langclaw.config.schema import DocumentsConfig

#: Record fields that describe a document, in the order they're embedded.
EMBEDDED_FIELDS = ("doc_type", "sender", "receiver", "document_date", "summary", "filename")


def build_embeddings(config: DocumentsConfig) -> Any:
    """An embeddings client for ``config.embedding_model``.

    Raises:
        ValueError: no API key (``embedding_api_key`` or ``OPENROUTER_API_KEY``).
    """
    api_key = config.embedding_api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError(
            "Semantic search needs an API key: set LANGCLAW__DOCUMENTS__EMBEDDING_API_KEY "
            "or OPENROUTER_API_KEY."
        )
    return OpenAIEmbeddings(
        model=config.embedding_model,
        base_url=config.embedding_base_url,
        api_key=api_key,
        # OpenRouter/other endpoints take raw text; skip OpenAI's local tokenizer check.
        check_embedding_ctx_length=False,
    )


def document_text(record: dict[str, Any]) -> str:
    """The text a record is embedded from: its described fields, then extras."""
    parts = [f"{k}: {record[k]}" for k in EMBEDDED_FIELDS if record.get(k)]
    fields = record.get("fields") or {}
    if fields:
        parts.append(json.dumps(fields, ensure_ascii=False, default=str))
    return "\n".join(parts)
