"""Tests for the stage 1 freshness gate."""

from datetime import UTC, datetime, timedelta

from project_pilot.evaluation.freshness import evaluate_freshness
from project_pilot.models import PostedPrecision

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_minute_precise_fresh() -> None:
    result = evaluate_freshness(
        posted_at=NOW - timedelta(minutes=10),
        posted_at_precision=PostedPrecision.MINUTE,
        watermark=None,
        now=NOW,
        window_minutes=30,
    )
    assert result.is_fresh is True
    assert result.reason["signal"] == "posted_at"


def test_minute_precise_stale() -> None:
    result = evaluate_freshness(
        posted_at=NOW - timedelta(minutes=60),
        posted_at_precision=PostedPrecision.MINUTE,
        watermark=NOW - timedelta(minutes=15),
        now=NOW,
        window_minutes=30,
    )
    assert result.is_fresh is False
    assert result.reason["reason"] == "posted_at older than analysis window"


def test_gap_rule_fresh_when_recent_run() -> None:
    result = evaluate_freshness(
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        watermark=NOW - timedelta(minutes=15),
        now=NOW,
        window_minutes=30,
    )
    assert result.is_fresh is True
    assert result.reason["signal"] == "gap"


def test_gap_rule_stale_after_downtime() -> None:
    result = evaluate_freshness(
        posted_at=None,
        posted_at_precision=PostedPrecision.DAY,
        watermark=NOW - timedelta(minutes=120),
        now=NOW,
        window_minutes=30,
    )
    assert result.is_fresh is False
    assert result.reason["reason"] == "last successful run older than analysis window"


def test_day_precision_uses_gap_not_posted() -> None:
    result = evaluate_freshness(
        posted_at=NOW - timedelta(minutes=5),
        posted_at_precision=PostedPrecision.DAY,
        watermark=NOW - timedelta(minutes=200),
        now=NOW,
        window_minutes=30,
    )
    assert result.is_fresh is False
    assert result.reason["signal"] == "gap"


def test_no_watermark_not_fresh() -> None:
    result = evaluate_freshness(
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        watermark=None,
        now=NOW,
        window_minutes=30,
    )
    assert result.is_fresh is False
