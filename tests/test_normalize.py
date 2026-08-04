"""Tests for ingestion normalization."""

from datetime import UTC, date, datetime

from project_pilot.ingestion.normalize import (
    canonicalize_url,
    compute_url_hash,
    detect_language,
    extract_contact_person,
    extract_listing_title,
    html_to_text,
    is_onsite_only,
    looks_like_company,
    next_page_url,
    parse_german_date,
    parse_posted,
    remote_status_from_percent,
    resolve_contact_name,
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


def test_parse_german_date_invalid() -> None:
    assert parse_german_date("32.13.2026") is None
    assert parse_german_date("no date here") is None


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


def test_detect_language() -> None:
    assert detect_language("Wir suchen einen erfahrenen Entwickler für das Projekt") == "de"
    assert detect_language("We are looking for a senior engineer for this role") == "en"
    assert detect_language("Python asyncio PostgreSQL") is None  # no stopword signal
    assert detect_language("") is None


def test_looks_like_company() -> None:
    assert looks_like_company("Hays AG") is True
    assert looks_like_company("Acme GmbH") is True
    assert looks_like_company("Anna Kleinen") is False


def test_extract_contact_person() -> None:
    text = "... My contact at Hays: My contact person: Anna Kleinen Referencenumber: 42 ..."
    assert extract_contact_person(text) == "Anna Kleinen"
    assert extract_contact_person("Ihr Ansprechpartner: Max Mustermann bei uns") == "Max Mustermann"
    assert extract_contact_person("Ansprechpartner: Hays AG") is None  # company, not a person
    assert extract_contact_person("no contact here") is None


def test_resolve_contact_name() -> None:
    # structured contact naming a person wins over the text
    assert resolve_contact_name("Anna", "Kleinen", "Acme GmbH", "Ansprechpartner: Max Muster") == (
        "Anna Kleinen"
    )
    # company-like or company-equal structured values fall back to the text label
    assert resolve_contact_name("Hays", "AG", None, "Ansprechpartner: Max Muster") == "Max Muster"
    assert resolve_contact_name("Acme", None, "Acme", "Ansprechpartner: Max Muster") == "Max Muster"
    # nothing structured, nothing in the text
    assert resolve_contact_name(None, None, None, "no contact here") is None


def test_is_onsite_only() -> None:
    # 0% remote and no remote hint anywhere -> on-site only
    assert is_onsite_only(0, "München", "Vor-Ort-Einsatz beim Kunden") is True
    # 0% remote but description mentions remote -> NOT on-site only (agency default)
    assert is_onsite_only(0, "Remote, Deutschland", "Remote work possible") is False
    # genuine hybrid / remote
    assert is_onsite_only(60, "Berlin", "hybrid") is False
    assert is_onsite_only(None, "Berlin", "onsite") is False  # unknown percent -> keep


RECRUITER_MAIL = """Hallo,

Für einen unserer Kunden aus dem Energiesektor suchen wir aktuell einen erfahrenen
Fullstack TypeScript Developer (m/w/d) mit starkem Angular-Schwerpunkt für die
Weiterentwicklung einer komplexen Enterprise-Webanwendung.

Rahmendaten:
Position: Senior Fullstack TypeScript Developer (m/w/d)
Start: 01.09.2026
Einsatzort: remote
"""


def test_extract_listing_title_reads_the_label_in_a_recruiter_mail() -> None:
    # The mail opens with a greeting and prose, so the "Position:" line is the headline.
    assert extract_listing_title(RECRUITER_MAIL) == (
        "Senior Fullstack TypeScript Developer (m/w/d)"
    )


def test_extract_listing_title_prefers_a_real_headline_at_the_top() -> None:
    assert extract_listing_title("Senior Data Engineer (m/w/d)\n\nWir suchen ...") == (
        "Senior Data Engineer (m/w/d)"
    )
    assert extract_listing_title("# AI Engineer für RAG-Plattform\n\nText") == (
        "AI Engineer für RAG-Plattform"
    )


def test_extract_listing_title_skips_noise_before_the_headline() -> None:
    assert extract_listing_title("https://x.de/projekt/1\nSenior Backend Developer") == (
        "Senior Backend Developer"
    )
    assert extract_listing_title("-----\nSenior Cloud Architect\nText") == "Senior Cloud Architect"


def test_extract_listing_title_strips_a_leading_label_but_keeps_a_real_dash() -> None:
    assert extract_listing_title("Projekt - Cloud Migration mit AWS\nText") == (
        "Cloud Migration mit AWS"
    )
    assert extract_listing_title("Fullstack Developer - Angular Fokus\nText") == (
        "Fullstack Developer - Angular Fokus"
    )


def test_extract_listing_title_returns_none_without_a_headline() -> None:
    # Prose, a bare greeting, and empty text carry no title — the LLM names those.
    assert (
        extract_listing_title("Wir suchen jemanden, der uns hilft. Bitte melden Sie sich.") is None
    )
    assert extract_listing_title("Hallo,\n\nanbei die Details. Bitte melden.") is None
    assert extract_listing_title("") is None


def test_extract_listing_title_is_capped_and_collapsed() -> None:
    long_title = "Senior " + "Developer " * 30
    title = extract_listing_title(f"Position: {long_title}\nText")
    assert title is not None
    assert len(title) <= 120 and "  " not in title
