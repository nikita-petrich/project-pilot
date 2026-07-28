"""Inline (CID) e-mail signatures: HTML, plain-text fallback, and embedded images.

The signature lives outside the repository (it holds photo, phone number and other
personal data) and is pointed at by ``SIGNATURE_DIR``. Images are embedded into the
message itself rather than hosted, so they render without the recipient having to
allow remote content — which matters for a cold application.
"""

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from project_pilot.errors import ConfigError

# Shared with the mailer, which rewrites these placeholders to real Content-IDs.
CID_REF = re.compile(r"cid:([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass(frozen=True, slots=True)
class InlineImage:
    """An image carried inside the mail and referenced by the HTML as ``cid:<name>``."""

    name: str
    data: bytes
    maintype: str
    subtype: str


@dataclass(frozen=True, slots=True)
class Signature:
    """One language variant: HTML with ``cid:`` refs, text fallback, embedded images."""

    html: str
    text: str
    images: tuple[InlineImage, ...] = ()


@dataclass(frozen=True, slots=True)
class Signatures:
    """The configured signatures, chosen by draft language (mirrors ``CvAttachments``)."""

    de: Signature | None = None
    en: Signature | None = None

    def for_language(self, language: str | None) -> Signature | None:
        """The signature matching the draft language (English → EN, otherwise German)."""
        return self.en if language == "en" else self.de


def load_signatures(directory: Path | None) -> Signatures:
    """Read every signature variant and its images once, at startup.

    Returns empty signatures when nothing is configured; a configured but unusable
    directory aborts the process instead of silently sending unsigned mail.
    """
    if directory is None:
        return Signatures()
    if not directory.is_dir():
        raise ConfigError(f"SIGNATURE_DIR {directory} is not a directory")
    return Signatures(de=_load_variant(directory, "de"), en=_load_variant(directory, "en"))


def _load_variant(directory: Path, language: str) -> Signature | None:
    """Load ``signature.<lang>.html`` plus its mandatory ``.txt`` twin, or nothing."""
    html_path = directory / f"signature.{language}.html"
    text_path = directory / f"signature.{language}.txt"
    if not html_path.is_file():
        return None
    if not text_path.is_file():
        raise ConfigError(f"{html_path.name} has no plain-text fallback: {text_path} is missing")
    html = _read_text(html_path)
    return Signature(html=html, text=_read_text(text_path), images=_load_images(directory, html))


def _load_images(directory: Path, html: str) -> tuple[InlineImage, ...]:
    """Resolve every distinct ``cid:`` reference in ``html`` to a file in ``directory``."""
    names = dict.fromkeys(CID_REF.findall(html))
    return tuple(_load_image(directory, name) for name in names)


def _load_image(directory: Path, name: str) -> InlineImage:
    """Find ``<name>`` or ``<name>.<ext>`` and read it as an inline image."""
    path = _resolve_image(directory, name)
    mime, _ = mimetypes.guess_type(path.name)
    maintype, _, subtype = (mime or "application/octet-stream").partition("/")
    try:
        data = path.read_bytes()
    except OSError as err:
        raise ConfigError(f"cannot read signature image {path}: {err}") from err
    return InlineImage(name=name, data=data, maintype=maintype, subtype=subtype or "octet-stream")


def _resolve_image(directory: Path, name: str) -> Path:
    exact = directory / name
    if exact.is_file():
        return exact
    matches = sorted(path for path in directory.glob(f"{name}.*") if path.is_file())
    if not matches:
        raise ConfigError(
            f"signature references cid:{name} but no file named {name}[.ext] exists in {directory}"
        )
    return matches[0]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as err:
        raise ConfigError(f"cannot read signature file {path}: {err}") from err
