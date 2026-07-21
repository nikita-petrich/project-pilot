"""Tests for the SchedulerRunner."""

import asyncio

from project_pilot.scheduler import SchedulerRunner


async def _noop() -> None:
    return None


def test_configure_registers_interval_job() -> None:
    runner = SchedulerRunner(_noop, interval_minutes=15, jitter_seconds=10)
    runner.configure()
    assert runner.has_job() is True


async def test_run_forever_stops_on_request() -> None:
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1

    runner = SchedulerRunner(job, interval_minutes=15)
    task = asyncio.create_task(runner.run_forever(install_signals=False))
    await asyncio.sleep(0.05)
    runner.request_stop()
    await asyncio.wait_for(task, timeout=2)
    assert calls == 0  # the 15-min interval never fired in this window
