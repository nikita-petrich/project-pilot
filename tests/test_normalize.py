"""Tests for ingestion normalization."""

from datetime import UTC, date, datetime

import pytest

from project_pilot.ingestion.normalize import (
    canonicalize_url,
    compute_url_hash,
    html_to_text,
    next_page_url,
    parse_end,
    parse_german_date,
    parse_posted,
    parse_start,
    remote_status_from_percent,
    remote_status_from_text,
    start_from_parts,
)
from project_pilot.models import PostedPrecision, RemoteStatus

BASE = "https://www.freelancermap.de"


def test_canonicalize_strips_query_fragment_and_trailing_slash() -> None:
    assert (
        canonicalize_url("/projekt/x-1?utm=a#top", BASE)
        == "https://www.freelancermap.de/projekt/x-1"
    )
    assert (
        canonicalize_url("https://www.freelancermap.de/projekt/y-2/", BASE)
        == "https://www.freelancermap.de/projekt/y-2"
    )


def test_canonicalize_relative_resolves_against_base() -> None:
    assert canonicalize_url("/a/b", BASE) == "https://www.freelancermap.de/a/b"


def test_url_hash_stable_and_hex() -> None:
    digest = compute_url_hash("https://x/y")
    assert digest == compute_url_hash("https://x/y")
    assert len(digest) == 64


def test_parse_start_asap() -> None:
    assert parse_start("ab sofort") == (None, True)


def test_parse_start_date() -> None:
    assert parse_start("01.09.2026") == (date(2026, 9, 1), False)


def test_parse_start_keine_angabe() -> None:
    assert parse_start("keine Angabe") == (None, False)


def test_parse_end_keine_angabe() -> None:
    assert parse_end("keine Angabe") is None


def test_parse_end_date() -> None:
    assert parse_end("31.12.2026") == date(2026, 12, 31)


def test_parse_german_date_invalid() -> None:
    assert parse_german_date("32.13.2026") is None
    assert parse_german_date("no date here") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100 % Remote", RemoteStatus.REMOTE),
        ("Homeoffice", RemoteStatus.REMOTE),
        ("Nein, vor Ort", RemoteStatus.ONSITE),
        ("vor Ort", RemoteStatus.ONSITE),
        ("Hamburg (hybrid)", RemoteStatus.HYBRID),
        ("Muenchen", RemoteStatus.UNKNOWN),
    ],
)
def test_remote_status(text: str, expected: RemoteStatus) -> None:
    assert remote_status_from_text(text) == expected


def test_parse_posted_minute_precision() -> None:
    posted_at, precision = parse_posted("2026-07-21T09:12:00+02:00", "21.07.2026")
    assert precision == PostedPrecision.MINUTE
    assert posted_at == datetime(2026, 7, 21, 7, 12, tzinfo=UTC)


def test_parse_posted_day_precision() -> None:
    posted_at, precision = parse_posted(None, "20.07.2026")
    assert precision == PostedPrecision.DAY
    assert posted_at is not None
    assert posted_at.tzinfo is not None


def test_parse_posted_unknown() -> None:
    assert parse_posted(None, None) == (None, PostedPrecision.UNKNOWN)
    assert parse_posted(None, "kein datum") == (None, PostedPrecision.UNKNOWN)


def test_remote_status_from_percent() -> None:
    assert remote_status_from_percent(100) == RemoteStatus.REMOTE
    assert remote_status_from_percent(0) == RemoteStatus.ONSITE
    assert remote_status_from_percent(50) == RemoteStatus.HYBRID
    assert remote_status_from_percent(None) == RemoteStatus.UNKNOWN


def test_start_from_parts() -> None:
    assert start_from_parts(None, None, "ab sofort") == (None, True)
    assert start_from_parts(2026, 9, None) == (date(2026, 9, 1), False)
    assert start_from_parts(None, None, "keine Angabe") == (None, False)
    assert start_from_parts(2026, 13, None) == (None, False)  # invalid month, no crash


def test_html_to_text() -> None:
    assert html_to_text('<div class="ql-editor"><p>Hallo</p> <b>Welt</b></div>') == "Hallo Welt"
    assert html_to_text("") == ""


def test_next_page_url_increments_pagenr() -> None:
    assert next_page_url("https://x.de/projekte?query=a&pagenr=1") == (
        "https://x.de/projekte?query=a&pagenr=2"
    )
    # array params survive and pagenr is added when absent
    added = next_page_url("https://x.de/projekte?query=a")
    assert added.endswith("pagenr=2")
