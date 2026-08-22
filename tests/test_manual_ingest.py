"""Manual ingestion: dedupe keys, titles, and the provenance record."""

from datetime import UTC, datetime

from project_pilot.ingestion.client import BASE_URL
from project_pilot.ingestion.manual import (
    INGEST_URL_PREFIX,
    MANUAL_SOURCE,
    build_manual_listing,
    listing_key,
)
from project_pilot.ingestion.normalize import (
    canonicalize_listing_url,
    canonicalize_url,
    compute_url_hash,
)
from project_pilot.models import ListingOrigin, ListingStatus, RemoteStatus

NOW = datetime(2026, 8, 22, 9, 30, tzinfo=UTC)
MAIL = """Hallo Nikita,

Position: Senior AI Engineer (Python)

wir suchen für unseren Kunden einen Entwickler für RAG-Pipelines.
"""


def test_a_url_produces_the_scanners_own_key() -> None:
    url = "https://www.freelancermap.de/projekt/beispiel-12345/?utm_source=mail"
    external_url, url_hash = listing_key(text="egal", url=url)
    canonical = canonicalize_url(url, BASE_URL)
    assert external_url == canonical
    assert url_hash == compute_url_hash(canonical)


def test_text_without_a_url_keys_on_the_text() -> None:
    external_url, url_hash = listing_key(text=MAIL, url=None)
    assert external_url.startswith(INGEST_URL_PREFIX)
    # Whitespace differences are not a different listing.
    spaced = listing_key(text=f"\n  {MAIL}   \n", url=None)
    assert spaced == (external_url, url_hash)
    # Different text is.
    assert listing_key(text="Etwas ganz anderes", url=None)[1] != url_hash


def test_build_reads_the_headline_out_of_a_recruiter_mail() -> None:
    listing = build_manual_listing(text=MAIL, origin=ListingOrigin.MAIL, now=NOW)
    assert listing.title == "Senior AI Engineer (Python)"
    assert listing.source == "manual"
    assert listing.status is ListingStatus.NEW
    assert listing.remote_status is RemoteStatus.UNKNOWN
    assert listing.first_seen_at == NOW


def test_an_explicit_title_wins_over_the_heuristic() -> None:
    listing = build_manual_listing(
        text=MAIL, origin=ListingOrigin.CHAT, now=NOW, title="  Vom User benannt  "
    )
    assert listing.title == "Vom User benannt"


def test_text_without_any_headline_falls_back_to_the_first_line() -> None:
    listing = build_manual_listing(
        text="wir bräuchten da mal jemanden für ein kleines Projekt",
        origin=ListingOrigin.CHAT,
        now=NOW,
    )
    assert listing.title.startswith("wir bräuchten")


def test_provenance_is_recorded_in_raw() -> None:
    listing = build_manual_listing(
        text=MAIL,
        origin=ListingOrigin.IMAGE,
        now=NOW,
        url="https://www.freelancermap.de/projekt/x-1",
        company="ACME GmbH",
        note="Screenshot aus WhatsApp",
    )
    assert listing.origin is ListingOrigin.IMAGE
    assert listing.raw["company"] == "ACME GmbH"
    ingest = listing.raw["ingest"]
    assert isinstance(ingest, dict)
    assert ingest == {
        "origin": "image",
        "received_at": NOW.isoformat(),
        "note": "Screenshot aus WhatsApp",
        "supplied_url": "https://www.freelancermap.de/projekt/x-1",
    }


def test_a_relative_path_is_never_resolved_against_the_scraped_board() -> None:
    """A bare path could belong to any host — key on the text instead of guessing."""
    external_url, _ = listing_key(text=MAIL, url="/projekt/relativ-12345")
    assert external_url.startswith(INGEST_URL_PREFIX)
    assert canonicalize_listing_url("/projekt/relativ-12345") is None


def test_the_board_is_read_off_the_url_and_can_be_overridden() -> None:
    """Nothing here is bound to one platform: the URL names the board."""
    from_other_board = build_manual_listing(
        text=MAIL,
        origin=ListingOrigin.URL,
        now=NOW,
        url="https://www.linkedin.com/jobs/view/4242",
    )
    assert from_other_board.source == "linkedin"

    scraped_board = build_manual_listing(
        text=MAIL,
        origin=ListingOrigin.URL,
        now=NOW,
        url="https://www.freelancermap.de/projekt/x-1",
    )
    # Matches the scanner's own SOURCE_NAME, so both rows agree on the board.
    assert scraped_board.source == "freelancermap"

    explicit = build_manual_listing(text=MAIL, origin=ListingOrigin.MAIL, now=NOW, source="Hays")
    assert explicit.source == "Hays"

    no_url = build_manual_listing(text=MAIL, origin=ListingOrigin.CHAT, now=NOW)
    assert no_url.source == MANUAL_SOURCE
