"""Durable SQLite job queue.

Guarantees relied on by the worker:
  * enqueue is idempotent per (project, ref, before, after)
  * claim_next is atomic — two workers never claim the same row
  * jobs whose worker crashed are recovered by lease expiry
  * failures retry with backoff up to a configured attempt limit
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import connect, init_db, transaction
from app.models import Job, JobStatus, PushEvent

log = logging.getLogger(__name__)

_COLUMNS = """
    id, dedupe_key, project_id, project_name, repo_url, ref, before_sha, after_sha,
    author_name, author_username, commit_count, payload_json, status, attempt_count,
    created_at, started_at, completed_at, available_at, last_error
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job.model_validate(dict(row))


class JobQueue:
    """Queue operations over one SQLite database file."""

    def __init__(
        self,
        db_path: Path,
        *,
        max_attempts: int = 5,
        lease_seconds: int = 1800,
        retry_backoff_seconds: int = 30,
    ) -> None:
        self.db_path = db_path
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.retry_backoff_seconds = retry_backoff_seconds
        self._conn = connect(db_path)
        init_db(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> JobQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- producer
    def enqueue(self, event: PushEvent) -> tuple[Job, bool]:
        """Insert a push event. Returns (job, created).

        A repeated delivery of the same push resolves to the existing row
        instead of a duplicate analysis.
        """
        now = _now()
        payload = event.model_dump_json()
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO jobs (
                    dedupe_key, project_id, project_name, repo_url, ref,
                    before_sha, after_sha, author_name, author_username,
                    commit_count, payload_json, status, attempt_count,
                    created_at, available_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(dedupe_key) DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (
                    event.dedupe_key,
                    event.project_id,
                    event.project_name,
                    event.repo_url,
                    event.ref,
                    event.before_sha,
                    event.after_sha,
                    event.author_name,
                    event.author_username,
                    event.commit_count,
                    payload,
                    JobStatus.QUEUED.value,
                    _iso(now),
                    _iso(now),
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return _row_to_job(row), True
            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE dedupe_key = ?", (event.dedupe_key,)
            ).fetchone()
        return _row_to_job(existing), False

    # ------------------------------------------------------------- consumer
    def claim_next(self) -> Job | None:
        """Atomically claim the oldest runnable job, or return None."""
        now = _now()
        with transaction(self._conn) as conn:
            row = conn.execute(
                f"""
                UPDATE jobs
                   SET status = ?,
                       started_at = ?,
                       attempt_count = attempt_count + 1
                 WHERE id = (
                       SELECT id FROM jobs
                        WHERE status = ? AND available_at <= ?
                        ORDER BY id
                        LIMIT 1
                 )
                RETURNING {_COLUMNS}
                """,
                (JobStatus.PROCESSING.value, _iso(now), JobStatus.QUEUED.value, _iso(now)),
            ).fetchone()
        return _row_to_job(row) if row else None

    def complete(self, job_id: int) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "UPDATE jobs SET status=?, completed_at=?, last_error=NULL WHERE id=?",
                (JobStatus.COMPLETED.value, _iso(_now()), job_id),
            )

    def fail(self, job_id: int, error: str, *, permanent: bool = False) -> JobStatus:
        """Record a failure; re-queue with backoff unless attempts ran out.

        Returns the status the job ended up in.
        """
        message = error[:2000]
        now = _now()
        with transaction(self._conn) as conn:
            row = conn.execute("SELECT attempt_count FROM jobs WHERE id = ?", (job_id,)).fetchone()
            attempts = int(row["attempt_count"]) if row else self.max_attempts
            exhausted = permanent or attempts >= self.max_attempts
            if exhausted:
                conn.execute(
                    "UPDATE jobs SET status=?, completed_at=?, last_error=? WHERE id=?",
                    (JobStatus.FAILED.value, _iso(now), message, job_id),
                )
                return JobStatus.FAILED
            # Exponential backoff, capped so a transient outage still drains.
            delay = min(self.retry_backoff_seconds * (2 ** (attempts - 1)), 900)
            conn.execute(
                "UPDATE jobs SET status=?, available_at=?, last_error=?, started_at=NULL "
                "WHERE id=?",
                (
                    JobStatus.QUEUED.value,
                    _iso(now + timedelta(seconds=delay)),
                    message,
                    job_id,
                ),
            )
            return JobStatus.QUEUED

    # ------------------------------------------------------------- recovery
    def recover_stale_jobs(self) -> int:
        """Re-queue jobs left in `processing` by a crashed worker.

        Called at worker start and periodically. Jobs past the attempt limit
        are failed outright rather than looping forever.
        """
        cutoff = _iso(_now() - timedelta(seconds=self.lease_seconds))
        with transaction(self._conn) as conn:
            requeued = conn.execute(
                """
                UPDATE jobs
                   SET status=?, started_at=NULL,
                       last_error=COALESCE(last_error, 'recovered after worker restart')
                 WHERE status=?
                   AND attempt_count < ?
                   AND (started_at IS NULL OR started_at <= ?)
                """,
                (JobStatus.QUEUED.value, JobStatus.PROCESSING.value, self.max_attempts, cutoff),
            ).rowcount
            conn.execute(
                """
                UPDATE jobs
                   SET status=?, completed_at=?,
                       last_error='abandoned after exceeding attempt limit'
                 WHERE status=? AND attempt_count >= ? AND (started_at IS NULL OR started_at <= ?)
                """,
                (
                    JobStatus.FAILED.value,
                    _iso(_now()),
                    JobStatus.PROCESSING.value,
                    self.max_attempts,
                    cutoff,
                ),
            )
        if requeued:
            log.warning("recovered interrupted jobs", extra={"count": requeued})
        return requeued

    def recover_all_processing(self) -> int:
        """Force-recover every `processing` job, ignoring the lease.

        Safe only at worker startup, where no other worker can be running a
        job this process would steal.
        """
        with transaction(self._conn) as conn:
            return conn.execute(
                "UPDATE jobs SET status=?, started_at=NULL WHERE status=? AND attempt_count < ?",
                (JobStatus.QUEUED.value, JobStatus.PROCESSING.value, self.max_attempts),
            ).rowcount

    # --------------------------------------------------------------- queries
    def get(self, job_id: int) -> Job | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        counts = {status.value: 0 for status in JobStatus}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    def purge_completed(self, older_than_days: int = 30) -> int:
        cutoff = _iso(_now() - timedelta(days=older_than_days))
        with transaction(self._conn) as conn:
            return conn.execute(
                "DELETE FROM jobs WHERE status IN (?,?) AND completed_at < ?",
                (JobStatus.COMPLETED.value, JobStatus.FAILED.value, cutoff),
            ).rowcount

    # ----------------------------------------------------- webhook replay
    def record_delivery(self, delivery_id: str) -> bool:
        """Register a webhook delivery id. False if it was already seen."""
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO webhook_deliveries (delivery_id, received_at) VALUES (?,?) "
                "ON CONFLICT(delivery_id) DO NOTHING",
                (delivery_id, _iso(_now())),
            )
            return cursor.rowcount > 0

    def purge_deliveries(self, older_than_hours: int = 48) -> int:
        cutoff = _iso(_now() - timedelta(hours=older_than_hours))
        with transaction(self._conn) as conn:
            return conn.execute(
                "DELETE FROM webhook_deliveries WHERE received_at < ?", (cutoff,)
            ).rowcount
