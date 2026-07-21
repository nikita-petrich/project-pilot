"""Stage 1 freshness gate: is a newly seen listing recent enough to evaluate?"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from project_pilot.models import PostedPrecision


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    is_fresh: bool
    reason: dict[str, object]


def evaluate_freshness(
    *,
    posted_at: datetime | None,
    posted_at_precision: PostedPrecision,
    watermark: datetime | None,
    now: datetime,
    window_minutes: int,
) -> FreshnessResult:
    """Decide freshness. Prefer a minute-precise posted time; else the gap rule.

    Gap rule: a newly seen entry is fresh only if the last successful run was within
    the window, so a backlog picked up after downtime is persisted but not analysed.
    """
    window = timedelta(minutes=window_minutes)

    if posted_at is not None and posted_at_precision is PostedPrecision.MINUTE:
        gap = now - posted_at
        is_fresh = gap <= window
        reason: dict[str, object] = {
            "signal": "posted_at",
            "posted_at": posted_at.isoformat(),
            "gap_minutes": round(gap.total_seconds() / 60, 2),
            "window_minutes": window_minutes,
        }
        if not is_fresh:
            reason["reason"] = "posted_at older than analysis window"
        return FreshnessResult(is_fresh=is_fresh, reason=reason)

    if watermark is None:
        return FreshnessResult(
            is_fresh=False,
            reason={"signal": "gap", "reason": "no watermark (seed or first run)"},
        )

    gap = now - watermark
    is_fresh = gap <= window
    reason = {
        "signal": "gap",
        "watermark": watermark.isoformat(),
        "gap_minutes": round(gap.total_seconds() / 60, 2),
        "window_minutes": window_minutes,
    }
    if not is_fresh:
        reason["reason"] = "last successful run older than analysis window"
    return FreshnessResult(is_fresh=is_fresh, reason=reason)
