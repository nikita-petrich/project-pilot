"""Tests for the Drive-backed CV refresher (HTTP mocked with respx, never live)."""

from pathlib import Path

import httpx
import respx

from project_pilot.application.cv_drive import DriveCvRefresher, parse_folder_listing

_FIXTURES = Path(__file__).parent / "fixtures"
_LISTING_HTML = (_FIXTURES / "drive_folder_listing.html").read_text(encoding="utf-8")
_FOLDER_ID = "test_folder_id"
_LISTING_URL = f"https://drive.google.com/embeddedfolderview?id={_FOLDER_ID}"
_DE_PDF = b"%PDF-1.7\ngerman-cv-bytes\n%%EOF"
_EN_PDF = b"%PDF-1.7\nenglish-cv-bytes\n%%EOF"


def _download_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def test_parse_folder_listing_maps_names_to_ids() -> None:
    listing = parse_folder_listing(_LISTING_HTML)
    assert listing == {
        "CV-German.pdf": "de_file_id_0001",
        "CV-English.pdf": "en_file_id_0002",
    }


def test_parse_folder_listing_skips_entry_without_a_title() -> None:
    html = (
        '<div class="flip-entry" id="entry-abc">no title here</div>'
        '<div class="flip-entry" id="entry-def">'
        '<div class="flip-entry-title">Real.pdf</div></div>'
    )
    # The title-less entry drops out instead of stealing the next entry's name.
    assert parse_folder_listing(html) == {"Real.pdf": "def"}


@respx.mock
async def test_refresh_downloads_each_cv_by_name(tmp_path: Path) -> None:
    de = tmp_path / "CV-German.pdf"
    en = tmp_path / "CV-English.pdf"
    respx.get(_LISTING_URL).mock(return_value=httpx.Response(200, text=_LISTING_HTML))
    respx.get(_download_url("de_file_id_0001")).mock(
        return_value=httpx.Response(200, content=_DE_PDF)
    )
    respx.get(_download_url("en_file_id_0002")).mock(
        return_value=httpx.Response(200, content=_EN_PDF)
    )

    await DriveCvRefresher(folder_id=_FOLDER_ID, targets=[de, en]).refresh()

    assert de.read_bytes() == _DE_PDF
    assert en.read_bytes() == _EN_PDF


@respx.mock
async def test_refresh_keeps_cache_when_folder_is_unreachable(tmp_path: Path) -> None:
    de = tmp_path / "CV-German.pdf"
    de.write_bytes(b"%PDF-cached")
    respx.get(_LISTING_URL).mock(return_value=httpx.Response(503))

    await DriveCvRefresher(folder_id=_FOLDER_ID, targets=[de]).refresh()

    assert de.read_bytes() == b"%PDF-cached"


@respx.mock
async def test_refresh_keeps_cache_when_download_is_not_a_pdf(tmp_path: Path) -> None:
    de = tmp_path / "CV-German.pdf"
    de.write_bytes(b"%PDF-cached")
    respx.get(_LISTING_URL).mock(return_value=httpx.Response(200, text=_LISTING_HTML))
    # Drive can answer a download with an HTML interstitial instead of the file.
    respx.get(_download_url("de_file_id_0001")).mock(
        return_value=httpx.Response(200, text="<html>Sign in to continue</html>")
    )

    await DriveCvRefresher(folder_id=_FOLDER_ID, targets=[de]).refresh()

    assert de.read_bytes() == b"%PDF-cached"


@respx.mock
async def test_refresh_keeps_cache_when_name_is_absent(tmp_path: Path) -> None:
    other = tmp_path / "CV-French.pdf"  # not present in the folder listing
    other.write_bytes(b"%PDF-cached")
    respx.get(_LISTING_URL).mock(return_value=httpx.Response(200, text=_LISTING_HTML))

    await DriveCvRefresher(folder_id=_FOLDER_ID, targets=[other]).refresh()

    assert other.read_bytes() == b"%PDF-cached"


@respx.mock
async def test_refresh_keeps_cache_when_listing_page_changed_shape(tmp_path: Path) -> None:
    de = tmp_path / "CV-German.pdf"
    de.write_bytes(b"%PDF-cached")
    # A 200 with no flip-entry markup (Google changed the page) parses to nothing;
    # the refresh must be a no-op that keeps the cache, never a wipe or a misfetch.
    respx.get(_LISTING_URL).mock(return_value=httpx.Response(200, text="<html>changed</html>"))

    await DriveCvRefresher(folder_id=_FOLDER_ID, targets=[de]).refresh()

    assert de.read_bytes() == b"%PDF-cached"


@respx.mock
async def test_refresh_without_targets_makes_no_requests() -> None:
    route = respx.get(_LISTING_URL).mock(return_value=httpx.Response(200, text=_LISTING_HTML))

    await DriveCvRefresher(folder_id=_FOLDER_ID, targets=[]).refresh()

    assert not route.called
