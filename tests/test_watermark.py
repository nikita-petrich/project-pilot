"""Tests for the watermark pagination stop criterion."""

from datetime import UTC, datetime

from project_pilot.ingestion.parser import ListingSummary
from project_pilot.ingestion.watermark import evaluate_page
from project_pilot.models import PostedPrecision


def _summary(url_hash: str, posted: datetime | None) -> ListingSummary:
    return ListingSummary(
        external_url=f"https://x/{url_hash}",
        url_hash=url_hash,
        title=url_hash,
        posted_at=posted,
        posted_at_precision=PostedPrecision.MINUTE if posted else PostedPrecision.UNKNOWN,
    )


def test_seed_run_processes_all() -> None:
    decision = evaluate_page([_summary("a", None), _summary("b", None)], set(), None)
    assert [s.url_hash for s in decision.new_summaries] == ["a", "b"]
    assert decision.should_stop is False


def test_known_hash_stops() -> None:
    decision = evaluate_page([_summary("a", None), _summary("b", None)], {"b"}, None)
    assert [s.url_hash for s in decision.new_summaries] == ["a"]
    assert decision.should_stop is True


def test_older_than_watermark_stops() -> None:
    watermark = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    fresh = _summary("new", datetime(2026, 7, 21, 9, 0, tzinfo=UTC))
    stale = _summary("old", datetime(2026, 7, 21, 7, 0, tzinfo=UTC))
    decision = evaluate_page([fresh, stale], set(), watermark)
    assert [s.url_hash for s in decision.new_summaries] == ["new"]
    assert decision.should_stop is True
