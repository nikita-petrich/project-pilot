"""APScheduler AsyncIOScheduler daemon driving the scan interval."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

type ScanJob = Callable[[], Awaitable[object]]


class SchedulerRunner:
    """Runs a scan job on an interval with overlap protection, until asked to stop."""

    def __init__(
        self,
        job: ScanJob,
        *,
        interval_minutes: int,
        jitter_seconds: int = 30,
        job_id: str = "scan",
    ) -> None:
        self._job = job
        self._interval_minutes = interval_minutes
        self._jitter_seconds = jitter_seconds
        self._job_id = job_id
        self._scheduler = AsyncIOScheduler()
        self._stop = asyncio.Event()

    def configure(self) -> None:
        self._scheduler.add_job(
            self._job,
            "interval",
            minutes=self._interval_minutes,
            jitter=self._jitter_seconds,
            max_instances=1,
            coalesce=True,
            id=self._job_id,
        )

    def has_job(self) -> bool:
        return self._scheduler.get_job(self._job_id) is not None

    @property
    def stop_event(self) -> asyncio.Event:
        """The shutdown signal, shared with co-running loops (e.g. the Slack bot)."""
        return self._stop

    def request_stop(self) -> None:
        self._stop.set()

    async def run_forever(self, *, install_signals: bool = True) -> None:
        self.configure()
        if install_signals:
            self._install_signal_handlers()
        self._scheduler.start()
        logger.info("scheduler started: scanning every %d min", self._interval_minutes)
        try:
            await self._stop.wait()
        finally:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler stopped")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop)
