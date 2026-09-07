"""Long-running analyzer worker.

Claims jobs from the durable SQLite queue and runs the analysis pipeline.
Designed to be restarted at any moment: interrupted jobs are recovered, and
SIGTERM finishes the job in flight before exiting.

Run with:  python -m app.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from app.analyzer import ChangeAnalyzer
from app.errors import PermanentJobError, UnknownRevisionError
from app.git_repo import short
from app.llm import build_provider
from app.logging_setup import configure_logging
from app.models import Job, JobStatus
from app.queue import JobQueue
from app.settings import Settings, get_settings
from app.slack import SlackNotifier

log = logging.getLogger(__name__)

# How often to re-check for jobs abandoned by a crashed worker.
MAINTENANCE_INTERVAL_SECONDS = 300


class Worker:
    """Owns the claim → analyse → complete loop for one process."""

    def __init__(
        self,
        settings: Settings,
        *,
        queue: JobQueue | None = None,
        analyzer: ChangeAnalyzer | None = None,
    ) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()
        self._owns_resources = analyzer is None

        settings.ensure_directories()
        self.queue = queue or JobQueue(
            settings.db_path,
            max_attempts=settings.max_job_attempts,
            lease_seconds=settings.job_lease_seconds,
            retry_backoff_seconds=settings.retry_backoff_seconds,
        )
        if analyzer is not None:
            self.analyzer = analyzer
        else:
            self._llm = build_provider(settings)
            self._slack = SlackNotifier(
                settings.slack_webhook_url.get_secret_value()
                if settings.slack_webhook_url
                else None,
                timeout=settings.slack_timeout_seconds,
            )
            self.analyzer = ChangeAnalyzer(settings, llm=self._llm, slack=self._slack)

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            log.info("shutdown requested; finishing current job")
            self._shutdown.set()

    # ------------------------------------------------------------------ loop
    async def run(self) -> None:
        log.info(
            "worker started",
            extra={
                "provider": self.settings.llm_provider,
                "data_dir": str(self.settings.bot_data_dir),
                "max_attempts": self.settings.max_job_attempts,
            },
        )
        # Nothing else can be running at startup, so reclaim every stuck job.
        if recovered := self.queue.recover_all_processing():
            log.warning("re-queued interrupted jobs at startup", extra={"count": recovered})

        last_maintenance = time.monotonic()
        try:
            while not self._shutdown.is_set():
                if time.monotonic() - last_maintenance > MAINTENANCE_INTERVAL_SECONDS:
                    self._maintenance()
                    last_maintenance = time.monotonic()

                job = self.queue.claim_next()
                if job is None:
                    await self._sleep(self.settings.worker_poll_seconds)
                    continue
                await self.process(job)
        finally:
            await self.aclose()
            log.info("worker stopped")

    async def _sleep(self, seconds: float) -> None:
        """Poll delay that returns immediately on shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)

    def _maintenance(self) -> None:
        self.queue.recover_stale_jobs()
        self.queue.purge_deliveries()
        self.queue.purge_completed()

    # --------------------------------------------------------------- one job
    async def process(self, job: Job) -> None:
        """Run one job, recording completion or failure. Never raises."""
        started = time.monotonic()
        context = {
            "job_id": job.id,
            "project_id": job.project_id,
            "project": job.project_name,
            "branch": job.ref.removeprefix("refs/heads/"),
            "before": short(job.before_sha),
            "after": short(job.after_sha),
            "attempt": job.attempt_count,
        }
        log.info("job started", extra=context)
        try:
            event = job.push_event()
            outcome = await self.analyzer.analyze(event)
        except (PermanentJobError, UnknownRevisionError) as exc:
            # Re-running cannot help: the commits are simply not reachable.
            self.queue.fail(job.id, str(exc), permanent=True)
            log.error("job failed permanently", extra={**context, "error": str(exc)[:500]})
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            status = self.queue.fail(job.id, f"{type(exc).__name__}: {exc}")
            level = log.error if status is JobStatus.FAILED else log.warning
            level(
                "job failed",
                extra={**context, "error": str(exc)[:500], "final": status is JobStatus.FAILED},
                exc_info=log.isEnabledFor(logging.DEBUG),
            )
        else:
            self.queue.complete(job.id)
            log.info(
                "job completed",
                extra={
                    **context,
                    "outcome": outcome.status,
                    "changed_files": outcome.changed_files,
                    "diff_chars": outcome.diff_chars,
                    "context_chars": outcome.context_chars,
                    "llm_calls": outcome.llm_calls,
                    "llm_ms": outcome.llm_ms,
                    "slack": outcome.notified,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )

    async def aclose(self) -> None:
        if self._owns_resources:
            await self._llm.aclose()
            await self._slack.aclose()
        self.queue.close()


async def main_async() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    worker = Worker(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_shutdown)

    await worker.run()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl-C is a normal exit
        asyncio.run(main_async())


if __name__ == "__main__":
    main()
