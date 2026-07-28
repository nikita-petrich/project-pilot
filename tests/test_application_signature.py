"""Tests for loading inline (CID) signatures from the configured directory."""

from pathlib import Path

import pytest

from project_pilot.application.signature import Signature, Signatures, load_signatures
from project_pilot.errors import ConfigError

_HTML = '<table><tr><td><img src="cid:photo"></td><td>Nikita Petrich</td></tr></table>'


def _variant(directory: Path, language: str, html: str = _HTML) -> None:
    (directory / f"signature.{language}.html").write_text(html, encoding="utf-8")
    (directory / f"signature.{language}.txt").write_text(f"Nikita Petrich ({language})\n")


def test_no_directory_configured_yields_empty_signatures() -> None:
    signatures = load_signatures(None)
    assert signatures.de is None
    assert signatures.en is None


def test_loads_html_text_and_referenced_image(tmp_path: Path) -> None:
    _variant(tmp_path, "de")
    (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8jpeg")

    signature = load_signatures(tmp_path).de

    assert signature is not None
    assert "Nikita Petrich" in signature.html
    assert signature.text == "Nikita Petrich (de)"
    assert [(i.name, i.maintype, i.subtype) for i in signature.images] == [
        ("photo", "image", "jpeg")
    ]
    assert signature.images[0].data == b"\xff\xd8jpeg"


def test_each_cid_is_loaded_once_even_when_referenced_twice(tmp_path: Path) -> None:
    _variant(tmp_path, "de", html='<img src="cid:logo"><img src="cid:logo">')
    (tmp_path / "logo.png").write_bytes(b"png")

    signature = load_signatures(tmp_path).de

    assert signature is not None
    assert [image.name for image in signature.images] == ["logo"]


def test_image_may_be_referenced_with_its_extension(tmp_path: Path) -> None:
    _variant(tmp_path, "de", html='<img src="cid:logo.png">')
    (tmp_path / "logo.png").write_bytes(b"png")

    signature = load_signatures(tmp_path).de

    assert signature is not None
    assert [image.name for image in signature.images] == ["logo.png"]


def test_signature_without_images_loads(tmp_path: Path) -> None:
    _variant(tmp_path, "de", html="<div>Nikita Petrich</div>")

    signature = load_signatures(tmp_path).de

    assert signature is not None
    assert signature.images == ()


def test_only_the_present_language_is_loaded(tmp_path: Path) -> None:
    _variant(tmp_path, "en", html="<div>EN</div>")

    signatures = load_signatures(tmp_path)

    assert signatures.de is None
    assert signatures.en is not None


def test_missing_text_fallback_aborts(tmp_path: Path) -> None:
    (tmp_path / "signature.de.html").write_text("<div>x</div>", encoding="utf-8")

    with pytest.raises(ConfigError, match="plain-text fallback"):
        load_signatures(tmp_path)


def test_unresolvable_cid_reference_aborts(tmp_path: Path) -> None:
    _variant(tmp_path, "de")

    with pytest.raises(ConfigError, match="cid:photo"):
        load_signatures(tmp_path)


def test_missing_directory_aborts(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a directory"):
        load_signatures(tmp_path / "nope")


def test_for_language_mirrors_cv_selection() -> None:
    de = Signature(html="<de>", text="de")
    en = Signature(html="<en>", text="en")
    signatures = Signatures(de=de, en=en)

    assert signatures.for_language("en") is en
    assert signatures.for_language("de") is de
    assert signatures.for_language(None) is de  # unknown language falls back to German
