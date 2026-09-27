"""Text extraction for documents in the bucket."""

from __future__ import annotations

import io

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv")
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json", ".xml", ".html", ".eml")


def extract_text(data: bytes, *, content_type: str = "", filename: str = "") -> tuple[str, str]:
    """Return ``(text, note)`` for a file.

    PDFs are read page by page (pypdf); text-like files are decoded as UTF-8.
    Anything else — and scanned PDFs, which have no text layer — returns empty
    text with a *note* saying why (OCR is not supported yet).
    """
    name = filename.lower()
    kind = (content_type or "").lower()
    if kind == "application/pdf" or name.endswith(".pdf") or data[:5] == b"%PDF-":
        return _pdf_text(data)
    if kind.startswith(TEXT_TYPES) or name.endswith(TEXT_SUFFIXES):
        return data.decode("utf-8", errors="replace"), ""
    return "", f"No text extraction for {content_type or 'this file type'} (OCR isn't supported)."


def _pdf_text(data: bytes) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "Reading PDFs needs the documents extra: uv add 'langclaw[documents]'"
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 — a broken PDF is a note, not a crash
        return "", f"Could not read the PDF: {exc}"
    text = "\n\n".join(f"[page {i}]\n{p}" for i, p in enumerate(pages, 1) if p)
    if not text:
        return "", "The PDF has no text layer (probably scanned; OCR isn't supported)."
    return text, ""
