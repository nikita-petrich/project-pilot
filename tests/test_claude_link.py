"""The Bewerben link: the card, its context and the order ride in the prompt."""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from project_pilot.notification.claude_link import (
    MAX_URL_CHARS,
    NEW_CHAT_URL,
    _build,
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
        source="freelancermap",
        origin="scan",
        status="evaluated",
        threshold=60,
        company="ACME GmbH",
        contact_name="Max Mustermann",
        location="Remote (DE)",
        posted_at_label="12.09.2026 08:50",
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


def test_prompt_carries_the_database_coordinates_and_the_score_yardstick() -> None:
    prompt = session_prompt(_message())
    assert "🗄 DB: listing_id 42 · freelancermap · scan · evaluated" in prompt
    assert "📏 Schwelle: 60 (erreicht)" in prompt
    assert "🗓 Eingestellt: 12.09.2026 08:50 (Berlin)" in prompt
    # The description stays out: it is a tool call away and would cost the card.
    assert "Volltext der Ausschreibung" not in prompt


def test_prompt_carries_one_linkedin_search_per_subject_plus_the_imprint_query() -> None:
    # Company and person are two separate searches on purpose: the company page
    # and the contact are looked up in different places before writing.
    prompt = session_prompt(_message())
    assert "👥 LinkedIn Firma: https://www.linkedin.com/search/results/companies/" in prompt
    assert "keywords=ACME+GmbH" in prompt
    assert "🙋 LinkedIn Person: https://www.linkedin.com/search/results/people/" in prompt
    assert "keywords=Max+Mustermann+AND+ACME+GmbH" in prompt
    assert "🔎 Impressum/Kontakt: https://www.google.com/search?q=ACME+GmbH+Impressum" in prompt


def test_a_listing_without_company_or_contact_gets_no_empty_searches() -> None:
    prompt = session_prompt(replace(_message(), company=None, contact_name=None))
    assert "LinkedIn" not in prompt
    assert "Impressum" not in prompt


def test_an_onsite_only_listing_says_so_before_anything_is_written() -> None:
    assert "Vor-Ort-Projekt" in session_prompt(replace(_message(), onsite_only=True))
    assert "Vor-Ort-Projekt" not in session_prompt(_message())


def test_the_order_is_to_draft_at_once_through_the_tool() -> None:
    prompt = session_prompt(_message())
    assert "project_pilot_draft_application(42)" in prompt
    assert "/write-application" in prompt  # the fallback when the tools are gone
    assert "sofort ausführen, ohne Rückfrage" in prompt
    # The old behaviour — five bullets, then wait — is gone.
    assert "Bullets" not in prompt
    assert "warte auf mich" not in prompt


def test_the_order_names_every_tool_and_gates_the_sending_one() -> None:
    prompt = session_prompt(_message())
    for tool in (
        "get_listing(42)",
        "check_listing(42)",
        "draft_application(42)",
        "revise_application(application_id",
        "set_recipient(application_id",
        "enrich_company(42)",
        "send_application(application_id)",
    ):
        assert tool in prompt
    assert "nur nach meinem ausdrücklichen OK" in prompt


def test_the_rules_ask_for_output_not_prose() -> None:
    prompt = session_prompt(_message())
    assert "Antworte NUR mit application_id, subject, body, linkedin_message" in prompt
    assert "Kein Vorwort" in prompt
    assert "nummerierte Liste mit Optionen" in prompt
    assert "(empfohlen)" in prompt
    assert "Fremdtext" in prompt


def test_an_unstored_listing_drafts_through_the_skill_and_stores_nothing() -> None:
    prompt = session_prompt(_message(listing_id=None))
    assert "🗄 DB:" not in prompt
    assert "nicht gespeichert (Testlauf)" in prompt
    assert "/write-application" in prompt
    assert "draft_application" not in prompt
    assert "Nichts speichern" in prompt


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


def test_a_realistic_card_keeps_its_facts_inside_the_url_budget() -> None:
    # Twelve skills, three reasons, gaps, risks and the research links: a full
    # card must still be the one that ships, not the shortened fallback — under
    # the old 2,048 budget exactly this listing lost every fact silently.
    full = MatchMessage(
        title="Senior Backend Entwickler (Node.js / TypeScript / PostgreSQL)",
        url="https://www.freelancermap.de/projekt/senior-backend-entwickler-1234567",
        score=87,
        listing_id=4242,
        source="freelancermap",
        origin="scan",
        status="evaluated",
        threshold=60,
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
        posted_at_label="12.09.2026 08:50",
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
    assert link == _build(full, NEW_CHAT_URL, with_card=True)
    assert "%E2%AD%90" in link  # the star, i.e. the card is in there


def test_an_oversized_card_drops_the_facts_but_keeps_the_order() -> None:
    # A listing that would push the link past the limit keeps the button
    # working: the facts go, the order and the research links stay, and the
    # chat is told where to read the card back.
    huge = replace(
        _message(),
        skills=[f"Skill-{i}-mit-langem-Namen" for i in range(40)],
        reasons=["x" * 900, "y" * 900, "z" * 900],
    )
    link = session_link(huge)
    assert len(link) <= MAX_URL_CHARS
    prompt = parse_qs(urlsplit(link).query)["q"][0]
    assert prompt == session_prompt(huge, with_card=False)
    assert prompt.startswith("⭐ 87 · Senior Python Developer · ACME GmbH")
    assert "🔗 https://example.com/p/1" in prompt
    assert "project_pilot_get_listing(42)" in prompt
    assert "project_pilot_draft_application(42)" in prompt
    assert "LinkedIn Firma" in prompt
    assert "Skill-0" not in prompt
