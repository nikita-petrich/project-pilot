"""Extract plain text from an uploaded document (PDF, image, or text) for drafting.

Used when a project description arrives as a Slack file upload instead of pasted
text — the extracted text feeds the same ``draft_from_text`` flow. PDFs and text
files are parsed locally; a screenshot is transcribed by a vision model, injected as
a ``VisionClient`` so this module stays free of the OpenAI SDK.
"""

import asyncio
from io import BytesIO
from typing import Protocol

from project_pilot.errors import ApplicationStateError

# The image formats a screenshot realistically arrives in, mapped to the MIME type
# the vision model is handed. Anything else falls through to the PDF/text path.
IMAGE_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
# A screenshot is well under this; the cap keeps a stray photo or scan from being
# base64-encoded into a model request.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class VisionClient(Protocol):
    """Transcribes image bytes (``application.vision.OpenAiVisionClient`` satisfies it)."""

    async def read_image(self, *, data: bytes, mime_type: str) -> str: ...


def image_mime_type(filename: str) -> str | None:
    """The MIME type for ``filename`` if it is a supported image, else ``None``."""
    return IMAGE_MIME_TYPES.get(_suffix(filename))


async def extract_upload_text(
    filename: str, data: bytes, *, vision: VisionClient | None = None
) -> str:
    """Return the readable text of any upload, transcribing images via ``vision``.

    PDFs and text files are parsed in a worker thread (the parsing is blocking);
    images go to the vision model. Raises ``ApplicationStateError`` with a hint the
    user can act on whenever nothing readable comes out.
    """
    mime_type = image_mime_type(filename)
    if mime_type is None:
        return await asyncio.to_thread(extract_document_text, filename, data)
    if vision is None:
        raise ApplicationStateError(
            "I can't read images right now — set OPENAI_API_KEY and LLM_MODEL (or "
            "VISION_MODEL) so screenshots get transcribed, or upload a PDF or text file."
        )
    if len(data) > MAX_IMAGE_BYTES:
        raise ApplicationStateError(
            f"This image is too large ({len(data) // (1024 * 1024)} MB, limit "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB) — crop it or paste the text instead."
        )
    try:
        text = await vision.read_image(data=data, mime_type=mime_type)
    except Exception as err:
        raise ApplicationStateError(f"Could not read this image: {err}") from err
    if not text.strip():
        raise ApplicationStateError(
            "No text found in this image — send a sharper screenshot or paste the "
            "project description as text."
        )
    return text.strip()


def extract_document_text(filename: str, data: bytes) -> str:
    """Return the readable text of ``data``; raise ``ApplicationStateError`` if unusable.

    PDFs are parsed page by page; anything else is treated as UTF-8 text. Binary
    files that are neither (null bytes / empty result) are rejected with a hint.
    Images never reach here — ``extract_upload_text`` routes them to the vision model.
    """
    is_pdf = _suffix(filename) == "pdf"
    raw = _extract_pdf(data) if is_pdf else data.decode("utf-8", errors="ignore")
    text = raw.strip()
    if not text or "\x00" in text:
        raise ApplicationStateError(
            "Could not read any text from this file — please attach a PDF, an image, "
            "or a text file with the project description."
        )
    return text


def _suffix(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except (PyPdfError, ValueError) as err:
        raise ApplicationStateError(f"PDF could not be read: {err}") from err
