"""Extract plain text from an uploaded document (PDF or text) for drafting.

Used when a project description arrives as a Slack file upload instead of pasted
text — the extracted text feeds the same ``draft_from_text`` flow.
"""

from io import BytesIO

from project_pilot.errors import ApplicationStateError


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
            "Could not read any text from this file — please attach a PDF or a text "
            "file with the project description."
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
