"""Tests for uploaded-document text extraction (PDF or text → draft input)."""

import pytest

from project_pilot.application.documents import extract_document_text
from project_pilot.errors import ApplicationStateError


def test_extract_plain_text_file() -> None:
    assert extract_document_text("desc.txt", b"Fullstack gesucht") == "Fullstack gesucht"


def test_extract_file_without_suffix_is_treated_as_text() -> None:
    assert extract_document_text("snippet", b"just some text") == "just some text"


def test_binary_without_readable_text_is_rejected() -> None:
    with pytest.raises(ApplicationStateError, match="PDF or"):
        extract_document_text("logo.png", b"\x89PNG\x00\x00binary")


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ApplicationStateError):
        extract_document_text("empty.txt", b"   \n  ")


def test_unreadable_pdf_raises_domain_error() -> None:
    with pytest.raises(ApplicationStateError, match="PDF"):
        extract_document_text("desc.pdf", b"not really a pdf")
