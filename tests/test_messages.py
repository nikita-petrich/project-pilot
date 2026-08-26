"""The transport-neutral match body: every fact, then the verdict."""

from project_pilot.notification.messages import MatchMessage, headline, render_match_details


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
