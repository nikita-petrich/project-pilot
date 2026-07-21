"""Tests for the freelancermap parser against the synthetic fixtures."""

from pathlib import Path

import pytest

from project_pilot.errors import SelectorMismatchError
from project_pilot.ingestion.parser import parse_detail_page, parse_list_page
from project_pilot.models import PostedPrecision, RemoteStatus

BASE = "https://www.freelancermap.de"
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_list_page_returns_all_cards() -> None:
    summaries = parse_list_page(_fixture("freelancermap_list.html"), BASE)
    assert len(summaries) == 4

    first = summaries[0]
    assert (
        first.external_url
        == "https://www.freelancermap.de/projekt/senior-python-entwickler-backend-12345"
    )
    assert len(first.url_hash) == 64
    assert first.posted_at_precision == PostedPrecision.MINUTE

    # absolute href + trailing slash canonicalizes to match the detail page URL
    assert (
        summaries[1].external_url
        == "https://www.freelancermap.de/projekt/data-engineer-azure-67890"
    )


def test_parse_list_page_raises_on_bad_html() -> None:
    with pytest.raises(SelectorMismatchError):
        parse_list_page("<html><body>nothing here</body></html>", BASE)


def test_parse_detail_asap_remote() -> None:
    parsed = parse_detail_page(
        _fixture("freelancermap_detail_asap_remote.html"),
        BASE,
        source="freelancermap",
        external_url="https://www.freelancermap.de/projekt/senior-python-entwickler-backend-12345",
    )
    assert parsed.title.startswith("Senior Python")
    assert parsed.start_asap is True
    assert parsed.start_date is None
    assert parsed.remote_status == RemoteStatus.REMOTE
    assert parsed.posted_at_precision == PostedPrecision.MINUTE
    assert "Python" in parsed.skills
    assert parsed.location == "Remote (Deutschland)"


def test_parse_detail_dated_onsite() -> None:
    parsed = parse_detail_page(
        _fixture("freelancermap_detail_dated_onsite.html"),
        BASE,
        source="freelancermap",
        external_url="https://www.freelancermap.de/projekt/data-engineer-azure-67890",
    )
    assert parsed.start_asap is False
    assert parsed.start_date is not None
    assert parsed.start_date.year == 2026
    assert parsed.end_date is None  # "keine Angabe"
    assert parsed.remote_status == RemoteStatus.ONSITE
    assert parsed.posted_at_precision == PostedPrecision.DAY


def test_parse_detail_raises_on_bad_html() -> None:
    with pytest.raises(SelectorMismatchError):
        parse_detail_page(
            "<html><body>x</body></html>",
            BASE,
            source="freelancermap",
            external_url="https://x/y",
        )
