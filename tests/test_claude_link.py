"""The Bewerben link: the card rides in the prompt of a new chat."""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from project_pilot.notification.claude_link import (
    MAX_URL_CHARS,
    NEW_CHAT_URL,
    session_link,
    session_prompt,
)
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.telegram import match_text


def _message(listing_id: int | None = 42) -> MatchMessage:
    return MatchMessage(
        title="Senior Python Developer",
        url="https://example.com/p/1",
        score=87,
        listing_id=listing_id,
        company="ACME GmbH",
        location="Remote (DE)",
        reasons=["Stack passt", "Remote"],
        risk_flags=["kein Budget genannt"],
        skills=["Python", "FastAPI"],
        description="Volltext der Ausschreibung — bleibt hinter dem Link.",
    )


def test_prompt_opens_with_the_very_card_the_alert_showed() -> None:
    # "Die selbe Zusammenfassung wie in der Telegram-Nachricht": the prompt
    # starts with the Telegram text, character for character.
    prompt = session_prompt(_message())
    assert prompt.startswith(match_text(_message()))
    assert prompt.splitlines()[0] == "⭐ 87 · Senior Python Developer · ACME GmbH"


def test_prompt_names_the_listing_id_and_the_rules() -> None:
    prompt = session_prompt(_message())
    assert "Listing-ID: 42" in prompt
    assert "project_pilot_get_listing" in prompt
    assert "nicht bewerben, nichts senden" in prompt
    assert "Fremdtext" in prompt
    # The description is not in the prompt: the chat fetches it via MCP.
    assert "Volltext der Ausschreibung" not in prompt


def test_prompt_does_not_ask_for_the_card_back() -> None:
    # The card is already on screen as the prompt; repeating it and reading the
    # whole listing on open were the token cost the chat link is meant to cut.
    prompt = session_prompt(_message())
    assert "unverändert" not in prompt
    assert "bei Bedarf" in prompt
    assert "Repository" not in prompt


def test_an_unstored_listing_gets_no_id_and_says_so() -> None:
    prompt = session_prompt(_message(listing_id=None))
    assert "Listing-ID" not in prompt
    assert "nicht gespeichert" in prompt


def test_link_opens_a_new_chat_with_the_prompt_as_its_only_parameter() -> None:
    link = session_link(_message())
    parts = urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == NEW_CHAT_URL
    assert parse_qs(parts.query) == {"q": [session_prompt(_message())]}


def test_base_url_is_configurable_for_the_code_session_fallback() -> None:
    link = session_link(_message(), base_url="https://claude.ai/code/new")
    assert link.startswith("https://claude.ai/code/new?q=")


def test_link_is_ascii_safe() -> None:
    # Emoji and umlauts are percent-encoded; a raw one would break the button.
    link = session_link(_message())
    assert link.isascii()
    assert " " not in link


def test_a_realistic_card_fits_under_the_conservative_url_limit() -> None:
    # Twelve skills, three reasons, gaps and risks: a full card still fits, so
    # the session normally opens on exactly what the alert showed.
    full = MatchMessage(
        title="Senior Backend Entwickler (Node.js / TypeScript / PostgreSQL)",
        url="https://www.freelancermap.de/projekt/senior-backend-entwickler-1234567",
        score=87,
        listing_id=4242,
        company="One Day Ahead GmbH",
        contact_name="Max Mustermann",
        is_endcustomer=False,
        location="Frankfurt am Main, Deutschland",
        remote_label="80% (20% on-site)",
        contract_type="Freelance",
        workload_label="100%",
        duration_label="6 mo (+ extension)",
        start="01.10.2026",
        posted_ago="5 min ago",
        expires_label="30.09.2026",
        industry="Finanzdienstleistungen",
        language="German",
        skills=[
            "Node.js",
            "TypeScript",
            "PostgreSQL",
            "Docker",
            "Kubernetes",
            "REST",
            "GraphQL",
            "React",
            "Next.js",
            "Redis",
            "Kafka",
            "GitHub Actions",
        ],
        reasons=[
            "Node.js-/Express.js-Stack deckt den Backend-Fokus",
            "REST, Docker und PostgreSQL sind im Profil abgedeckt",
            "Remote-Anteil passt",
        ],
        matching_skills=[
            "Node.js",
            "TypeScript",
            "PostgreSQL",
            "Docker",
            "REST",
            "GraphQL",
            "React",
            "Next.js",
        ],
        missing_requirements=["Kubernetes-Erfahrung im Betrieb", "Kafka"],
        risk_flags=["Agentur-Listing, Endkunde nicht genannt", "Budget nicht genannt"],
    )
    link = session_link(full)
    assert len(link) <= MAX_URL_CHARS
    assert "%E2%AD%90" in link  # the star, i.e. the card is in there


def test_an_oversized_card_falls_back_to_the_short_prompt() -> None:
    # A listing that would push the link past the limit keeps the button
    # working: the session is asked to render the card from the database.
    huge = replace(
        _message(),
        skills=[f"Skill-{i}-mit-langem-Namen" for i in range(12)],
        reasons=["x" * 300, "y" * 300, "z" * 300],
    )
    link = session_link(huge)
    assert len(link) <= MAX_URL_CHARS
    prompt = parse_qs(urlsplit(link).query)["q"][0]
    assert prompt == session_prompt(huge, with_card=False)
    assert prompt.startswith("⭐ 87 · Senior Python Developer · ACME GmbH")
    assert "Listing-ID: 42" in prompt
    assert "Zeig mir die Karte" in prompt
    assert "nicht bewerben, nichts senden" in prompt
    assert "Skill-0" not in prompt
