"""Turn Slack uploads (PDF, text, or image) into drafting input.

Used when a project description arrives as a Slack file upload instead of pasted
text — documents are extracted to text for the ``draft_from_text`` flow, while
images travel as ``ImageAttachment`` payloads straight into the vision-capable
LLM call (drafting and revision alike).
"""

from dataclasses import dataclass, field
from io import BytesIO

from project_pilot.errors import ApplicationStateError

# Image formats the OpenAI vision input accepts; anything else is not an image
# attachment and falls back to the document-extraction path.
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """An uploaded image (screenshot of a listing, feedback, …) for the LLM."""

    name: str
    mime_type: str
    data: bytes = field(repr=False)  # keep byte blobs out of logs


def is_image_mime_type(mime_type: str | None) -> bool:
    """True when ``mime_type`` is an image format the vision LLM accepts."""
    return mime_type in IMAGE_MIME_TYPES


def extract_document_text(filename: str, data: bytes) -> str:
    """Return the readable text of ``data``; raise ``ApplicationStateError`` if unusable.

    PDFs are parsed page by page; anything else is treated as UTF-8 text. Binary
    files that are neither (null bytes / empty result) are rejected with a hint.
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    raw = _extract_pdf(data) if suffix == "pdf" else data.decode("utf-8", errors="ignore")
    text = raw.strip()
    if not text or "\x00" in text:
        raise ApplicationStateError(
            "Could not read any text from this file — please attach a PDF, a text "
            "file, or an image (PNG/JPEG/WebP/GIF) with the project description."
        )
    return text


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except (PyPdfError, ValueError) as err:
        raise ApplicationStateError(f"PDF could not be read: {err}") from err
