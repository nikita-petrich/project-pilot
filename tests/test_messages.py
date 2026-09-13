"""The transport-neutral match body: every fact, then the verdict."""

from dataclasses import replace
from datetime import UTC, datetime

from project_pilot.models import (
    Evaluation,
    EvaluationStage,
    Listing,
    ListingOrigin,
    ListingStatus,
    RemoteStatus,
    Verdict,
)
from project_pilot.notification.messages import (
    MatchMessage,
    from_stored,
    headline,
    render_card,
    render_match_details,
)


def _full() -> MatchMessage:
    return MatchMessage(
        title="Senior Backend Entwickler",
        url="https://example.com/p/9",
        score=87,
        company="One Day Ahead GmbH",
        contact_name="Paul Franzke",
        is_endcustomer=False,
        location="Frankfurt am Main, Deutschland",
        remote_label="80% (20% on-site)",
        contract_type="Freelance",
        workload_label="100%",
        duration_label="4 mo (+ extension)",
        start="01.09.2026",
        posted_ago="5 min ago",
        expires_label="30.09.2026",
        industry="IT & Software",
        language="German",
        skills=["Node.js", "REST", "Docker"],
        reasons=["Node.js-Stack passt", "REST und Docker abgedeckt", "Remote passt"],
        matching_skills=["Node.js", "REST"],
        missing_requirements=["Kubernetes"],
        risk_flags=["kein Budget genannt"],
        description="Volltext der Ausschreibung.",
    )


def test_every_listing_fact_gets_its_own_labelled_line() -> None:
    """The set Slack showed, in the order it showed it."""
    facts = render_match_details(_full()).split("\n\n")[0].splitlines()
    assert facts == [
        "🏢 Company: One Day Ahead GmbH",
        "👤 Contact: Paul Franzke",
        "🤝 Client type: Agency",
        "📍 Location: Frankfurt am Main, Deutschland",
        "🏠 Remote: 80% (20% on-site)",
        "💼 Contract: Freelance",
        "📊 Workload: 100%",
        "⏳ Duration: 4 mo (+ extension)",
        "📅 Start: 01.09.2026",
        "🕒 Posted: 5 min ago",
        "✍️ Apply by: 30.09.2026",
        "🏭 Industry: IT & Software",
        "🗣 Language: German",
        "🛠 Skills: Node.js, REST, Docker",
    ]


def test_the_verdict_follows_the_facts_as_its_own_block() -> None:
    verdict = render_match_details(_full()).split("\n\n")[1].splitlines()
    assert verdict == [
        "🎯 Score: 87/100",
        "✅ Fits: Node.js-Stack passt, REST und Docker abgedeckt, Remote passt",
        "🎯 Your skills: Node.js, REST",
        "⚠️ Gaps: Kubernetes",
        "🚩 Risks: kein Budget genannt",
    ]


def test_the_description_stays_out_of_the_message() -> None:
    """It rides behind its own button; inline it would bury the facts."""
    assert "Volltext" not in render_match_details(_full())


def test_the_link_closes_the_message() -> None:
    assert render_match_details(_full()).endswith("🔗 https://example.com/p/9")


def test_an_unnamed_company_location_and_industry_are_stated_not_dropped() -> None:
    """An agency post that hides its client is a signal, not a blank."""
    details = render_match_details(
        MatchMessage(title="Rolle", url="https://example.com/p/1", score=61)
    )
    assert "🏢 Company: not stated" in details
    assert "📍 Location: not stated" in details
    assert "🏭 Industry: unknown" in details
    # Nothing was said about the terms, so those lines are simply absent.
    assert "📅 Start" not in details
    assert "✅ Fits" not in details


def test_details_drop_the_link_line_without_a_url() -> None:
    """A listing checked from pasted text has no link — an empty 🔗 is noise."""
    assert "🔗" not in render_match_details(MatchMessage(title="Rolle", url="", score=70))


def test_headline_names_score_role_and_company() -> None:
    """This line becomes the topic's name, so it carries the identifying three."""
    message = MatchMessage(
        title="Senior Backend Entwickler", url="https://x/1", score=87, company="ACME GmbH"
    )
    assert headline(message) == "⭐ 87 · Senior Backend Entwickler · ACME GmbH"
    # A listing that names no company still yields a usable name.
    assert headline(MatchMessage(title="Rolle", url="", score=61)) == "⭐ 61 · Rolle"


def _stored_listing() -> Listing:
    """One scanned listing, posted at 06:50 UTC — 08:50 Berlin in September."""
    listing = Listing(
        source="freelancermap",
        external_url="https://example.com/p/9",
        url_hash="hash",
        title="Senior Backend Entwickler",
        description="Node.js und REST.",
        status=ListingStatus.EVALUATED,
        remote_status=RemoteStatus.REMOTE,
        origin=ListingOrigin.SCAN,
        posted_at=datetime(2026, 9, 12, 6, 50, tzinfo=UTC),
        first_seen_at=datetime(2026, 9, 12, 6, 55, tzinfo=UTC),
        last_seen_at=datetime(2026, 9, 12, 6, 55, tzinfo=UTC),
        raw={"company": "One Day Ahead GmbH"},
    )
    listing.evaluations.append(
        Evaluation(
            stage=EvaluationStage.LLM,
            verdict=Verdict.MATCH,
            score=87,
            reason={"reasons": ["passt"]},
            created_at=datetime(2026, 9, 12, 6, 51, tzinfo=UTC),
        )
    )
    return listing


def test_a_stored_listing_carries_its_database_coordinates() -> None:
    # The chat has to name the row it works on; guessing an id is not an option.
    message = from_stored(_stored_listing(), datetime(2026, 9, 12, 7, 0, tzinfo=UTC))
    assert (message.source, message.origin, message.status) == (
        "freelancermap",
        "scan",
        "evaluated",
    )


def test_the_posting_time_is_rendered_absolute_in_berlin_time() -> None:
    # "5 min ago" is true when the alert arrives and wrong by the evening; the
    # absolute stamp is what a chat opened hours later can still read.
    message = from_stored(_stored_listing(), datetime(2026, 9, 12, 7, 0, tzinfo=UTC))
    assert message.posted_at_label == "12.09.2026 08:50"
    assert message.posted_ago == "10 min ago"


def test_the_threshold_travels_with_the_score_or_stays_unset() -> None:
    now = datetime(2026, 9, 12, 7, 0, tzinfo=UTC)
    assert from_stored(_stored_listing(), now, threshold=60).threshold == 60
    assert from_stored(_stored_listing(), now).threshold is None


def _card_message() -> MatchMessage:
    return MatchMessage(
        title="Senior Python Developer",
        url="https://example.com/p/1",
        score=87,
        listing_id=42,
        source="freelancermap",
        origin="scan",
        status="evaluated",
        threshold=60,
        company="ACME GmbH",
        contact_name="Max Mustermann",
        location="Remote (DE)",
        posted_at_label="12.09.2026 08:50",
        reasons=["Stack passt"],
        skills=["Python"],
    )


def test_the_card_carries_the_database_coordinates_and_the_score_yardstick() -> None:
    card = render_card(_card_message())
    assert "🗄 DB: listing_id 42 · freelancermap · scan · evaluated" in card
    assert "📏 Schwelle: 60 (erreicht)" in card
    assert "🗓 Eingestellt: 12.09.2026 08:50 (Berlin)" in card


def test_the_card_carries_one_search_per_subject() -> None:
    # Company and person are two separate searches on purpose: the company page
    # and the contact are looked up in different places before writing.
    card = render_card(_card_message())
    assert "👥 LinkedIn Firma: https://www.linkedin.com/search/results/companies/" in card
    assert "keywords=ACME+GmbH" in card
    assert "🙋 LinkedIn Person: https://www.linkedin.com/search/results/people/" in card
    assert "keywords=Max+Mustermann+AND+ACME+GmbH" in card
    assert "🔎 Impressum/Kontakt: https://www.google.com/search?q=ACME+GmbH+Impressum" in card


def test_a_missing_contact_is_stated_rather_than_dropped() -> None:
    # An ad that names nobody is a fact worth seeing at a glance — the same
    # reason the card prints "Company: not stated" instead of hiding the line.
    card = render_card(replace(_card_message(), contact_name=None))
    assert "🙋 LinkedIn Person: K.A." in card
    assert "search/results/people" not in card


def test_a_listing_without_a_company_gets_no_empty_searches() -> None:
    # Without a name there is nothing to search for, so those two lines go.
    card = render_card(replace(_card_message(), company=None, contact_name=None))
    assert "LinkedIn Firma" not in card
    assert "Impressum" not in card
    assert "🙋 LinkedIn Person: K.A." in card


def test_an_onsite_only_listing_says_so_before_anything_is_written() -> None:
    assert "Vor-Ort-Projekt" in render_card(replace(_card_message(), onsite_only=True))
    assert "Vor-Ort-Projekt" not in render_card(_card_message())


def test_the_card_opens_with_the_very_text_the_alert_showed() -> None:
    # One layout for three surfaces: the alert, the chat prompt and the skill.
    message = _card_message()
    assert render_card(message).startswith(
        f"{headline(message)}\n\n{render_match_details(message)}"
    )
