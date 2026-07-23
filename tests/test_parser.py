"""Tests for the freelancermap parser against the react-on-rails JSON fixtures."""

from datetime import date
from pathlib import Path

import pytest

from project_pilot.errors import SelectorMismatchError
from project_pilot.ingestion.parser import parse_detail_page, parse_list_page
from project_pilot.models import PostedPrecision, RemoteStatus

BASE = "https://www.freelancermap.de"
FIXTURES = Path(__file__).parent / "fixtures"

_EMPTY_LIST = (
    '<script class="js-react-on-rails-component" data-component-name="ProjectSearch">'
    '{"initialResults":[]}</script>'
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_list_page_returns_all_results() -> None:
    summaries = parse_list_page(_fixture("freelancermap_list.html"), BASE)
    assert len(summaries) == 3

    first = summaries[0]
    assert (
        first.external_url
        == "https://www.freelancermap.de/projekt/senior-python-entwickler-backend-12345"
    )
    assert len(first.url_hash) == 64
    assert first.title == "Senior Python Entwickler (Backend)"
    assert first.posted_at_precision == PostedPrecision.MINUTE

    assert (
        summaries[1].external_url
        == "https://www.freelancermap.de/projekt/data-engineer-azure-67890"
    )


def test_parse_list_page_empty_results_is_valid() -> None:
    assert parse_list_page(_EMPTY_LIST, BASE) == []


def test_parse_list_page_raises_when_component_missing() -> None:
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
    assert parsed.location == "Remote, Deutschland"
    assert "asyncio" in parsed.description
    assert parsed.raw["id"] == 12345  # full source record preserved for losslessness


def test_parse_detail_dated_onsite() -> None:
    parsed = parse_detail_page(
        _fixture("freelancermap_detail_dated_onsite.html"),
        BASE,
        source="freelancermap",
        external_url="https://www.freelancermap.de/projekt/data-engineer-azure-67890",
    )
    assert parsed.start_asap is False
    assert parsed.start_date == date(2026, 9, 1)
    assert parsed.end_date is None
    assert parsed.remote_status == RemoteStatus.ONSITE
    assert parsed.posted_at_precision == PostedPrecision.MINUTE
    assert parsed.location == "München, Deutschland"


def test_parse_detail_raises_when_component_missing() -> None:
    with pytest.raises(SelectorMismatchError):
        parse_detail_page(
            "<html><body>x</body></html>",
            BASE,
            source="freelancermap",
            external_url="https://x/y",
        )
