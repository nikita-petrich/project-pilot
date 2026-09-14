"""ProfileService: the website's half, the repo's half, and the hash over both."""

from pathlib import Path

import pytest

from project_pilot.errors import ConfigError
from project_pilot.profile_loader import ProfileService
from project_pilot.profile_source import ProfileFeed, RemoteProfile

_FEED = {
    "name": "Nikita Petrich",
    "role": "Senior Full-Stack & AI Engineer",
    "availability": {
        "from": "immediately",
        "capacity_percent": 100,
        "onsite_max_percent": 30,
        "employee_leasing": True,
    },
    "rate": {"currency": "EUR", "basis": "from", "hourly": 80, "daily": 640, "label": {}},
    "contact": {
        "email": "n.petrich@sequenz.io",
        "phone": "+49 15679088678",
        "web": "https://sequenz.io",
        "booking": {"de": "https://cal.example/de", "en": "https://cal.example/en"},
    },
    "location": {"de": "München, Deutschland", "en": "Munich, Germany"},
    "vat_id": "DE368159064",
    "profiles": [
        {"label": "LinkedIn", "href": "https://linkedin.com/in/nikita-petrich"},
        {"label": "GitHub", "href": "https://github.com/nikita-petrich"},
    ],
    "sentences": {
        "availability": {"de": "Ab sofort in Vollzeit.", "en": "Immediately, full time."},
        "rate": {"de": "Ab 80 € pro Stunde.", "en": "From €80 per hour."},
    },
}


class _FakeSite:
    """The website, without the network."""

    def __init__(self, markdown: str = "# Me\n\nPython engineer.", feed: object = None) -> None:
        self.markdown = markdown
        self._feed = feed if feed is not None else _FEED
        self.calls = 0

    async def fetch(self) -> RemoteProfile:
        self.calls += 1
        return RemoteProfile(
            markdown=self.markdown,
            feed=ProfileFeed.model_validate(self._feed),
            source_url="https://sequenz.io/en.md",
        )


_PRIVATE = "blacklist:\n  - wordpress\nmust_have:\n  - python\nlanguages: [de, en]\n"


def _private(tmp_path: Path, yaml_text: str = _PRIVATE) -> Path:
    (tmp_path / "private.yaml").write_text(yaml_text, encoding="utf-8")
    return tmp_path


def _service(tmp_path: Path, site: _FakeSite | None = None) -> ProfileService:
    return ProfileService(tmp_path, site or _FakeSite())


async def test_the_profile_is_the_website_plus_the_private_half(tmp_path: Path) -> None:
    _private(tmp_path, _PRIVATE + "no_gos: |\n  ## No-gos\n\n  - Defence\n")
    profile = await _service(tmp_path).load()

    assert "Python engineer." in profile.text  # the fetched half
    assert "## No-gos" in profile.text and "Defence" in profile.text  # the private half
    assert profile.constraints.blacklist == ["wordpress"]
    assert profile.constraints.must_have == ["python"]
    assert profile.source_url == "https://sequenz.io/en.md"
    assert len(profile.profile_hash) == 64


async def test_the_no_gos_never_reach_the_public_half(tmp_path: Path) -> None:
    # The whole point of splitting the profile: a company homepage does not say
    # which clients are refused. That half is read from the repo, never fetched.
    site = _FakeSite()
    _private(tmp_path, _PRIVATE + "no_gos: |\n  ## No-gos\n\n  - Defence\n")
    await _service(tmp_path, site).load()
    assert "Defence" not in site.markdown


async def test_the_figures_feed_becomes_exact_key_value_lines(tmp_path: Path) -> None:
    # The application prompt asks for a VAT id and a booking link per language by
    # name, so they must be values, not phrases inside a sentence.
    _private(tmp_path)
    profile = await _service(tmp_path).load()

    assert profile.applicant_name() == "Nikita Petrich"
    assert profile.contact_value("Phone") == "+49 15679088678"
    assert profile.contact_value("Email") == "n.petrich@sequenz.io"
    assert profile.contact_value("CTA German") == "https://cal.example/de"
    assert profile.contact_value("CTA English") == "https://cal.example/en"
    assert profile.contact_value("Location German") == "München, Deutschland"
    assert profile.contact_value("VAT ID") == "DE368159064"
    assert profile.contact_value("LinkedIn") == "https://linkedin.com/in/nikita-petrich"
    assert "Capacity: 100% (of which at most 30% on-site)" in profile.text
    assert "Rate: from 80 EUR/h · from 640 EUR/day" in profile.text
    assert "Rate sentence (German): Ab 80 € pro Stunde." in profile.text


async def test_hash_changes_with_the_fetched_text(tmp_path: Path) -> None:
    # The profile now changes without a deploy, so the hash has to follow the
    # website — otherwise two different profiles would file verdicts under one id.
    _private(tmp_path)
    first = await ProfileService(tmp_path, _FakeSite("A")).load()
    second = await ProfileService(tmp_path, _FakeSite("B")).load()
    assert first.profile_hash != second.profile_hash


async def test_hash_changes_with_the_private_half(tmp_path: Path) -> None:
    _private(tmp_path)
    first = await _service(tmp_path).load()
    _private(tmp_path, _PRIVATE + "nogo_technologies:\n  - java\n")
    second = await _service(tmp_path).load()
    assert first.profile_hash != second.profile_hash


async def test_hash_stable_for_the_same_inputs(tmp_path: Path) -> None:
    _private(tmp_path)
    assert (await _service(tmp_path).load()).profile_hash == (
        await _service(tmp_path).load()
    ).profile_hash


async def test_missing_private_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        await _service(tmp_path).load()


async def test_invalid_private_type_raises(tmp_path: Path) -> None:
    _private(tmp_path, "blacklist: not-a-list\n")
    with pytest.raises(ConfigError):
        await _service(tmp_path).load()


async def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    _private(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        await _service(tmp_path).load()


REPO_ROOT = Path(__file__).resolve().parent.parent


async def test_the_shipped_private_half_keeps_the_guard_armed() -> None:
    """The repo's own private profile must still block Java and Spring."""
    profile = await ProfileService(REPO_ROOT / "profile", _FakeSite()).load()
    assert "java" in profile.constraints.nogo_technologies
    assert "spring" in profile.constraints.nogo_technologies
    # And the industries that can never be published stay in the text the LLM sees.
    assert "Defense / military / weapons" in profile.text
    assert "Adult industry" in profile.text


async def test_the_websites_own_contact_section_does_not_win(tmp_path: Path) -> None:
    # Found end to end, not by a unit test: the twin carries a contact block of
    # its own, and reading the first matching section meant the site silently
    # decided what went into an outgoing signature — with half the keys missing.
    site = _FakeSite(
        markdown=(
            "# Me\n\n## Contact & signature\n\n- **Name:** Someone Else\n- **Phone:** +49 000\n"
        )
    )
    _private(tmp_path)
    profile = await ProfileService(tmp_path, site).load()

    assert profile.applicant_name() == "Nikita Petrich"
    assert profile.contact_value("Phone") == "+49 15679088678"
    assert profile.contact_value("CTA German") == "https://cal.example/de"
