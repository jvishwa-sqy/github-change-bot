"""Durable queue semantics: dedupe, atomic claim, retry, crash recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import JobStatus, PushEvent
from app.queue import JobQueue


def make_event(after: str = "b" * 40, ref: str = "refs/heads/main") -> PushEvent:
    return PushEvent(
        project_id=42,
        project_name="acme/app",
        repo_url="git@github.com:acme/app.git",
        ref=ref,
        before_sha="a" * 40,
        after_sha=after,
        author_name="Vishwa",
        commit_count=2,
    )


@pytest.fixture
def queue(tmp_path):
    instance = JobQueue(tmp_path / "queue.sqlite3", max_attempts=3, retry_backoff_seconds=0)
    yield instance
    instance.close()


def test_enqueue_and_claim(queue: JobQueue) -> None:
    job, created = queue.enqueue(make_event())
    assert created
    assert job.status is JobStatus.QUEUED
    assert job.attempt_count == 0

    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status is JobStatus.PROCESSING
    assert claimed.attempt_count == 1


def test_claim_returns_none_when_empty(queue: JobQueue) -> None:
    assert queue.claim_next() is None


def test_duplicate_push_is_not_queued_twice(queue: JobQueue) -> None:
    first, created_first = queue.enqueue(make_event())
    second, created_second = queue.enqueue(make_event())
    assert created_first and not created_second
    assert first.id == second.id
    assert queue.counts()["queued"] == 1


def test_distinct_pushes_are_separate_jobs(queue: JobQueue) -> None:
    queue.enqueue(make_event())
    queue.enqueue(make_event(after="c" * 40))
    queue.enqueue(make_event(ref="refs/heads/other"))
    assert queue.counts()["queued"] == 3


def test_a_job_is_claimed_only_once(queue: JobQueue) -> None:
    queue.enqueue(make_event())
    assert queue.claim_next() is not None
    assert queue.claim_next() is None


def test_complete(queue: JobQueue) -> None:
    job, _ = queue.enqueue(make_event())
    queue.claim_next()
    queue.complete(job.id)

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
    assert stored.completed_at is not None
    assert stored.last_error is None


def test_failure_requeues_until_attempts_run_out(queue: JobQueue) -> None:
    job, _ = queue.enqueue(make_event())

    for attempt in range(1, 3):
        claimed = queue.claim_next()
        assert claimed is not None
        assert claimed.attempt_count == attempt
        assert queue.fail(job.id, f"boom {attempt}") is JobStatus.QUEUED

    claimed = queue.claim_next()
    assert claimed is not None and claimed.attempt_count == 3
    assert queue.fail(job.id, "final boom") is JobStatus.FAILED

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.last_error == "final boom"
    assert queue.claim_next() is None


def test_permanent_failure_skips_remaining_attempts(queue: JobQueue) -> None:
    job, _ = queue.enqueue(make_event())
    queue.claim_next()
    assert queue.fail(job.id, "unknown revision", permanent=True) is JobStatus.FAILED
    assert queue.claim_next() is None


def test_retry_backoff_delays_the_next_claim(tmp_path) -> None:
    queue = JobQueue(tmp_path / "q.sqlite3", max_attempts=5, retry_backoff_seconds=600)
    job, _ = queue.enqueue(make_event())
    queue.claim_next()
    queue.fail(job.id, "transient")
    assert queue.claim_next() is None  # still backing off
    queue.close()


def test_recover_interrupted_job_at_startup(tmp_path) -> None:
    """A worker that dies mid-job must not strand it in `processing`."""
    path = tmp_path / "q.sqlite3"
    first = JobQueue(path, max_attempts=3)
    job, _ = first.enqueue(make_event())
    first.claim_next()
    first.close()  # simulate SIGKILL

    second = JobQueue(path, max_attempts=3)
    assert second.get(job.id).status is JobStatus.PROCESSING
    assert second.recover_all_processing() == 1

    reclaimed = second.claim_next()
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.attempt_count == 2
    second.close()


def test_lease_expiry_recovers_stale_jobs(tmp_path) -> None:
    queue = JobQueue(tmp_path / "q.sqlite3", max_attempts=3, lease_seconds=60)
    job, _ = queue.enqueue(make_event())
    queue.claim_next()

    assert queue.recover_stale_jobs() == 0  # lease still valid
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    queue._conn.execute("UPDATE jobs SET started_at = ?", (stale,))

    assert queue.recover_stale_jobs() == 1
    assert queue.claim_next() is not None
    queue.close()


def test_stale_job_past_attempt_limit_is_failed(tmp_path) -> None:
    queue = JobQueue(tmp_path / "q.sqlite3", max_attempts=1, lease_seconds=60)
    job, _ = queue.enqueue(make_event())
    queue.claim_next()
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    queue._conn.execute("UPDATE jobs SET started_at = ?", (stale,))

    queue.recover_stale_jobs()
    assert queue.get(job.id).status is JobStatus.FAILED
    queue.close()


def test_job_round_trips_the_push_event(queue: JobQueue) -> None:
    original = make_event()
    job, _ = queue.enqueue(original)
    assert queue.get(job.id).push_event() == original


def test_delivery_replay_guard(queue: JobQueue) -> None:
    assert queue.record_delivery("delivery-1") is True
    assert queue.record_delivery("delivery-1") is False
    assert queue.record_delivery("delivery-2") is True


def test_purge_helpers(queue: JobQueue) -> None:
    job, _ = queue.enqueue(make_event())
    queue.claim_next()
    queue.complete(job.id)
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    queue._conn.execute("UPDATE jobs SET completed_at = ?", (old,))
    assert queue.purge_completed(older_than_days=30) == 1

    queue.record_delivery("old-delivery")
    queue._conn.execute("UPDATE webhook_deliveries SET received_at = ?", (old,))
    assert queue.purge_deliveries(older_than_hours=1) == 1


def test_queue_survives_reopen(tmp_path) -> None:
    path = tmp_path / "q.sqlite3"
    first = JobQueue(path)
    first.enqueue(make_event())
    first.close()

    second = JobQueue(path)
    assert second.counts()["queued"] == 1
    assert second.claim_next() is not None
    second.close()
