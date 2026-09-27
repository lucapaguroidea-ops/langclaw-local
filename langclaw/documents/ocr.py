"""
OCR with a vision model — reads scanned PDFs and photos.

Used as a fallback: :func:`langclaw.documents.text.extract_text` handles files
with a text layer; when it finds none, :class:`VisionOcr` renders each PDF page
to an image (pypdfium2) — or takes a photo as is — and asks a vision-capable
chat model to transcribe it.

Enable with ``LANGCLAW__DOCUMENTS__OCR_MODEL`` (e.g.
``openrouter:google/gemini-2.5-flash``); unset ⇒ no OCR, and scans come back
with a note saying so.
"""

from __future__ import annotations

import base64
import io
from typing import Any

from langchain.chat_models import init_chat_model
from loguru import logger

#: Image types sent to the model as they are.
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
_IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".webp": "image/webp", ".gif": "image/gif"}  # fmt: skip

PROMPT = (
    "Transcribe all text in this document image exactly, in reading order. "
    "Keep line breaks and table rows; don't summarise or translate. "
    "If there is no text, reply with nothing."
)


def image_type(content_type: str = "", filename: str = "") -> str:
    """The image MIME type of a file, or ``""`` when it isn't an image we can send."""
    kind = (content_type or "").lower().split(";")[0].strip()
    if kind in IMAGE_TYPES:
        return kind
    name = filename.lower()
    return next((t for s, t in _IMAGE_SUFFIXES.items() if name.endswith(s)), "")


def is_pdf(data: bytes, content_type: str = "", filename: str = "") -> bool:
    return (
        (content_type or "").lower() == "application/pdf"
        or filename.lower().endswith(".pdf")
        or data[:5] == b"%PDF-"
    )


class VisionOcr:
    """Transcribe page images with a vision-capable chat model.

    Args:
        model: A LangChain chat model that accepts image content blocks.
        max_pages: Only the first *max_pages* PDF pages are read (bounds cost).
        scale: PDF render scale (2 ≈ 144 dpi — legible without huge images).
    """

    def __init__(self, model: Any, *, max_pages: int = 10, scale: float = 2.0) -> None:
        self.model = model
        self.max_pages = max_pages
        self.scale = scale

    @classmethod
    def from_spec(cls, spec: str, *, max_pages: int = 10) -> VisionOcr:
        """Build from a model spec such as ``openrouter:google/gemini-2.5-flash``."""
        return cls(init_chat_model(spec), max_pages=max_pages)

    async def read(
        self, data: bytes, *, content_type: str = "", filename: str = ""
    ) -> tuple[str, str]:
        """Return ``(text, note)``; failures come back as a *note*, never raised."""
        try:
            if is_pdf(data, content_type, filename):
                pages = self._render_pdf(data)
                texts = [await self._transcribe(png, "image/png") for png in pages]
                text = "\n\n".join(f"[page {i}]\n{t}" for i, t in enumerate(texts, 1) if t)
            elif kind := image_type(content_type, filename):
                text = await self._transcribe(data, kind)
            else:
                return "", f"OCR can't read {content_type or 'this file type'}."
        except Exception as exc:  # noqa: BLE001 — surfaced to the tool as a note
            logger.warning(f"OCR failed for {filename or content_type}: {exc}")
            return "", f"OCR failed: {exc}"
        return (text, "") if text else ("", "OCR found no text.")

    def _render_pdf(self, data: bytes) -> list[bytes]:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise RuntimeError(
                "reading scanned PDFs needs the documents extra: uv add 'langclaw[documents]'"
            ) from exc
        pdf = pdfium.PdfDocument(data)
        try:
            pngs = []
            for index in range(min(len(pdf), self.max_pages)):
                image = pdf[index].render(scale=self.scale).to_pil()
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                pngs.append(buf.getvalue())
            return pngs
        finally:
            pdf.close()

    async def _transcribe(self, image: bytes, mime: str) -> str:
        from langchain_core.messages import HumanMessage

        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        reply = await self.model.ainvoke(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": url}},
                    ]
                )
            ]
        )
        content = reply.content
        if isinstance(content, list):  # some providers return content blocks
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return str(content or "").strip()
