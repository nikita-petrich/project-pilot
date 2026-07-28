"""Tests for uploaded-document text extraction (PDF, image, or text → draft input)."""

import pytest

from project_pilot.application.documents import (
    MAX_IMAGE_BYTES,
    extract_document_text,
    extract_upload_text,
    image_mime_type,
)
from project_pilot.errors import ApplicationStateError


class _FakeVision:
    """Stands in for the vision model: records the call, returns a canned transcript."""

    def __init__(
        self, text: str = "Senior Python Engineer gesucht", error: Exception | None = None
    ) -> None:
        self.calls: list[tuple[int, str]] = []
        self.text = text
        self.error = error

    async def read_image(self, *, data: bytes, mime_type: str) -> str:
        self.calls.append((len(data), mime_type))
        if self.error is not None:
            raise self.error
        return self.text


def test_extract_plain_text_file() -> None:
    assert extract_document_text("desc.txt", b"Fullstack gesucht") == "Fullstack gesucht"


def test_extract_file_without_suffix_is_treated_as_text() -> None:
    assert extract_document_text("snippet", b"just some text") == "just some text"


def test_binary_without_readable_text_is_rejected() -> None:
    with pytest.raises(ApplicationStateError, match="PDF"):
        extract_document_text("archive.bin", b"\x00\x00binary")


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ApplicationStateError):
        extract_document_text("empty.txt", b"   \n  ")


def test_unreadable_pdf_raises_domain_error() -> None:
    with pytest.raises(ApplicationStateError, match="PDF"):
        extract_document_text("desc.pdf", b"not really a pdf")


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("screenshot.png", "image/png"),
        ("Foto.JPG", "image/jpeg"),
        ("shot.jpeg", "image/jpeg"),
        ("clip.webp", "image/webp"),
        ("desc.pdf", None),
        ("desc.txt", None),
        ("noSuffix", None),
    ],
)
def test_image_mime_type_detection(filename: str, expected: str | None) -> None:
    assert image_mime_type(filename) == expected


async def test_upload_text_reads_a_screenshot_through_the_vision_model() -> None:
    vision = _FakeVision()
    text = await extract_upload_text("screenshot.png", b"\x89PNG fake bytes", vision=vision)
    assert text == "Senior Python Engineer gesucht"
    assert vision.calls == [(15, "image/png")]  # the raw bytes reach the model as PNG


async def test_upload_text_still_reads_text_files_without_a_vision_model() -> None:
    assert await extract_upload_text("desc.txt", b"Fullstack gesucht") == "Fullstack gesucht"


async def test_screenshot_without_a_vision_model_explains_the_setting() -> None:
    with pytest.raises(ApplicationStateError, match="VISION_MODEL"):
        await extract_upload_text("screenshot.png", b"\x89PNG")


async def test_screenshot_the_model_finds_no_text_in_is_rejected() -> None:
    with pytest.raises(ApplicationStateError, match="No text found"):
        await extract_upload_text("screenshot.png", b"\x89PNG", vision=_FakeVision(text="  \n "))


async def test_vision_failure_becomes_a_domain_error() -> None:
    vision = _FakeVision(error=RuntimeError("rate limited"))
    with pytest.raises(ApplicationStateError, match="rate limited"):
        await extract_upload_text("screenshot.png", b"\x89PNG", vision=vision)


async def test_oversized_image_is_rejected_before_the_model_call() -> None:
    vision = _FakeVision()
    oversized = b"x" * (MAX_IMAGE_BYTES + 1)
    with pytest.raises(ApplicationStateError, match="too large"):
        await extract_upload_text("huge.png", oversized, vision=vision)
    assert vision.calls == []  # never base64-encoded into a request


async def test_transcription_is_stripped() -> None:
    vision = _FakeVision(text="\n  Projekt: RAG Pipeline  \n")
    assert await extract_upload_text("s.png", b"x", vision=vision) == "Projekt: RAG Pipeline"
