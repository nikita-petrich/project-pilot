"""Reading the profile off the website: what arrives, and what happens when it does not."""

import httpx
import pytest
import respx

from project_pilot.errors import ProfileUnavailableError
from project_pilot.profile_source import ProfileFeed, WebProfileSource, derived_sections

SITE = "https://sequenz.io"
TWIN_URL = f"{SITE}/en.md"
FEED_URL = f"{SITE}/api/profile.json"

TWIN = "# Nikita Petrich — Senior Full-Stack & AI Engineer\n\nPython, LLMs, RAG.\n"
FEED = {
    "name": "Nikita Petrich",
    "role": "Senior Full-Stack & AI Engineer",
    "availability": {
        "from": "immediately",
        "capacity_percent": 100,
        "onsite_max_percent": 30,
        "employee_leasing": True,
    },
    "rate": {"currency": "EUR", "basis": "from", "hourly": 80, "daily": 640},
    "contact": {
        "email": "n.petrich@sequenz.io",
        "phone": "+49 15679088678",
        "web": SITE,
        "booking": {"de": "https://cal.example/de", "en": "https://cal.example/en"},
    },
    "location": {"de": "München, Deutschland", "en": "Munich, Germany"},
    "vat_id": "DE368159064",
    "profiles": [{"label": "LinkedIn", "href": "https://linkedin.com/in/nikita-petrich"}],
    "sentences": {"rate": {"de": "Ab 80 €.", "en": "From €80."}},
}


def _source() -> WebProfileSource:
    return WebProfileSource(base_url=SITE, locale="en")


@respx.mock
async def test_both_documents_are_read() -> None:
    respx.get(TWIN_URL).respond(200, text=TWIN)
    respx.get(FEED_URL).respond(200, json=FEED)

    remote = await _source().fetch()

    assert "Python, LLMs, RAG." in remote.markdown
    assert remote.feed.availability.capacity_percent == 100
    assert remote.source_url == TWIN_URL


@respx.mock
async def test_a_missing_twin_is_fatal_not_a_fallback() -> None:
    # No silent fallback to an older copy by design: an evaluation carries the
    # hash of the profile it was judged against, and a stale profile would file
    # today's verdict under a text that was not in force.
    respx.get(TWIN_URL).respond(404)
    respx.get(FEED_URL).respond(200, json=FEED)

    with pytest.raises(ProfileUnavailableError, match=r"en\.md"):
        await _source().fetch()


@respx.mock
async def test_an_empty_twin_is_refused() -> None:
    # A deploy that serves the route but no content would otherwise pass as a
    # profile with nothing in it — and match every listing equally badly.
    respx.get(TWIN_URL).respond(200, text="   \n")
    respx.get(FEED_URL).respond(200, json=FEED)

    with pytest.raises(ProfileUnavailableError, match="empty"):
        await _source().fetch()


@respx.mock
async def test_a_feed_missing_its_figures_is_refused() -> None:
    respx.get(TWIN_URL).respond(200, text=TWIN)
    respx.get(FEED_URL).respond(200, json={"name": "x"})

    with pytest.raises(ProfileUnavailableError, match="profile feed"):
        await _source().fetch()


@respx.mock
async def test_a_flaky_edge_is_retried_before_it_counts_as_down() -> None:
    route = respx.get(TWIN_URL)
    route.side_effect = [httpx.ConnectError("reset"), httpx.Response(200, text=TWIN)]
    respx.get(FEED_URL).respond(200, json=FEED)

    remote = await _source().fetch()

    assert route.call_count == 2
    assert remote.markdown.strip() == TWIN.strip()


def test_the_figures_become_the_lines_the_prompt_asks_for_by_name() -> None:
    block = derived_sections(ProfileFeed.model_validate(FEED))
    assert "Capacity: 100% (of which at most 30% on-site)" in block
    assert "Rate: from 80 EUR/h · from 640 EUR/day · project-dependent" in block
    assert "CTA English: https://cal.example/en" in block
    assert "Location German: München, Deutschland" in block
    assert "VAT ID: DE368159064" in block
    assert "LinkedIn: https://linkedin.com/in/nikita-petrich" in block
    # A profile that does not list GitHub simply has no GitHub line — never an
    # empty label the signature would then render as "GitHub:".
    assert "GitHub:" not in block
