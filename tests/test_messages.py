"""The transport-neutral match card: what the overview looks like."""

from project_pilot.notification.messages import MatchMessage, render_match_card


def test_match_card_renders_the_scannable_overview() -> None:
    """The four fact lines Nik reads first — ported from the Slack card."""
    card = render_match_card(
        MatchMessage(
            title="Senior Backend Entwickler",
            url="https://example.com/p/9",
            score=87,
            company="One Day Ahead GmbH",
            is_endcustomer=False,
            location="Frankfurt am Main, Deutschland",
            remote_label="100%",
            start="01.09.2026",
            duration_label="4 mo (+ extension)",
            workload_label="100%",
            posted_ago="5 min ago",
            reasons=["Node.js-Stack passt", "REST und Docker abgedeckt", "dritter Grund"],
        )
    )
    assert card.splitlines() == [
        "🎯 Senior Backend Entwickler  ·  87/100",
        "🏢 One Day Ahead GmbH  ·  Agency",
        "📍 Frankfurt am Main, Deutschland  ·  🏠 100%",
        "📅 01.09.2026  ·  ⏳ 4 mo (+ extension)  ·  📊 100%  ·  🕒 5 min ago",
        # Only the top two reasons; the full list rides below the card.
        "✅ Fits: Node.js-Stack passt, REST und Docker abgedeckt",
        "🔗 https://example.com/p/9",
    ]


def test_match_card_states_a_missing_company_instead_of_dropping_the_line() -> None:
    """An agency post that hides its client is a signal, not a blank."""
    card = render_match_card(
        MatchMessage(title="Rolle", url="https://example.com/p/1", score=61, is_endcustomer=True)
    )
    assert "🏢 Company not stated" in card
    assert "📍 Location not stated" in card
    # No terms, no fit reasons — those lines are simply absent.
    assert "📅" not in card and "✅" not in card


def test_match_card_drops_the_link_line_without_a_url() -> None:
    """A listing checked from pasted text has no link — an empty 🔗 is noise."""
    card = render_match_card(MatchMessage(title="Rolle", url="", score=70))
    assert "🔗" not in card
