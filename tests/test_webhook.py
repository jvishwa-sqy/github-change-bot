"""End-to-end webhook receiver behaviour."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.queue import JobQueue
from app.security import compute_signature
from app.settings import Settings

SECRET = "test-webhook-secret"
ZERO = "0" * 40


def push_payload(**overrides) -> dict:
    payload = {
        "ref": "refs/heads/feature/dotcom-fix",
        "before": "a" * 40,
        "after": "b" * 40,
        "compare": "https://github.com/acme/app/compare/aaaaaaa...bbbbbbb",
        "created": False,
        "deleted": False,
        "repository": {
            "id": 987654,
            "full_name": "acme/ai-caller-core",
            "name": "ai-caller-core",
            "html_url": "https://github.com/acme/ai-caller-core",
            "clone_url": "https://github.com/acme/ai-caller-core.git",
            "ssh_url": "git@github.com:acme/ai-caller-core.git",
            "default_branch": "main",
        },
        "pusher": {"name": "vishwa"},
        "sender": {"login": "vishwa"},
        "head_commit": {
            "id": "b" * 40,
            "message": "Enable language tools for dotcom",
            "author": {"name": "Vishwa", "username": "vishwa"},
        },
        "commits": [
            {"id": "b" * 40, "message": "Enable language tools for dotcom"},
        ],
    }
    payload.update(overrides)
    return payload


def post(
    client: TestClient, payload: dict, *, event: str = "push", delivery: str = "d-1", **headers
):
    body = json.dumps(payload).encode()
    base = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": compute_signature(SECRET, body),
        "Content-Type": "application/json",
    }
    base.update(headers)
    return client.post("/webhooks/github", content=body, headers=base)


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def queue(settings) -> Iterator[JobQueue]:
    instance = JobQueue(settings.db_path)
    yield instance
    instance.close()


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_queue_depth(client: TestClient) -> None:
    body = client.get("/ready").json()
    assert body["status"] == "ok"
    assert body["queue"]["queued"] == 0


def test_valid_push_is_accepted_and_enqueued(client: TestClient, queue: JobQueue) -> None:
    response = post(client, push_payload())
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"

    job = queue.claim_next()
    assert job is not None
    assert job.project_id == 987654
    assert job.project_name == "acme/ai-caller-core"
    assert job.ref == "refs/heads/feature/dotcom-fix"
    assert job.before_sha == "a" * 40
    assert job.after_sha == "b" * 40
    assert job.author_name == "Vishwa"
    assert job.commit_count == 1

    event = job.push_event()
    assert event.branch == "feature/dotcom-fix"
    assert event.repo_url == "git@github.com:acme/ai-caller-core.git"
    assert event.compare_url.endswith("aaaaaaa...bbbbbbb")


def test_invalid_signature_is_rejected(client: TestClient, queue: JobQueue) -> None:
    body = json.dumps(push_payload()).encode()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d-bad",
            "X-Hub-Signature-256": compute_signature("wrong", body),
        },
    )
    assert response.status_code == 401
    assert queue.counts()["queued"] == 0


def test_unsigned_request_is_rejected(client: TestClient, queue: JobQueue) -> None:
    response = client.post(
        "/webhooks/github", json=push_payload(), headers={"X-GitHub-Event": "push"}
    )
    assert response.status_code == 401
    assert queue.counts()["queued"] == 0


def test_ping_event(client: TestClient) -> None:
    response = post(client, {"zen": "Design for failure."}, event="ping")
    assert response.status_code == 200
    assert response.json()["status"] == "pong"


def test_unsupported_event_is_ignored(client: TestClient, queue: JobQueue) -> None:
    response = post(client, push_payload(), event="issues")
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert queue.counts()["queued"] == 0


def test_duplicate_delivery_is_ignored(client: TestClient, queue: JobQueue) -> None:
    assert post(client, push_payload(), delivery="same").json()["status"] == "accepted"
    assert post(client, push_payload(), delivery="same").json()["status"] == "duplicate"
    assert queue.counts()["queued"] == 1


def test_same_push_from_a_new_delivery_is_deduplicated(client: TestClient, queue: JobQueue) -> None:
    post(client, push_payload(), delivery="one")
    body = post(client, push_payload(), delivery="two")
    assert body.json()["status"] == "duplicate"
    assert queue.counts()["queued"] == 1


def test_tag_push_is_ignored(client: TestClient, queue: JobQueue) -> None:
    response = post(client, push_payload(ref="refs/tags/v1.0.0"))
    assert response.json()["status"] == "ignored"
    assert queue.counts()["queued"] == 0


def test_branch_deletion_is_queued(client: TestClient, queue: JobQueue) -> None:
    """Deletion carries no commits but still deserves a notification."""
    response = post(
        client,
        push_payload(after=ZERO, deleted=True, commits=[], head_commit=None),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    job = queue.claim_next()
    assert job is not None and job.after_sha == ZERO


def test_branch_creation_is_queued(client: TestClient, queue: JobQueue) -> None:
    response = post(client, push_payload(before=ZERO, created=True), delivery="created")
    assert response.json()["status"] == "accepted"
    assert queue.claim_next().before_sha == ZERO


def test_empty_push_is_ignored(client: TestClient, queue: JobQueue) -> None:
    response = post(client, push_payload(commits=[], head_commit=None))
    assert response.json()["status"] == "ignored"
    assert queue.counts()["queued"] == 0


def test_malformed_payload_is_rejected(client: TestClient) -> None:
    response = post(client, {"not": "a push"})
    assert response.status_code == 400


def test_invalid_json_is_rejected(client: TestClient) -> None:
    body = b"{not json"
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d-json",
            "X-Hub-Signature-256": compute_signature(SECRET, body),
        },
    )
    assert response.status_code == 400


def test_legacy_token_is_accepted(settings, queue: JobQueue) -> None:
    configured = Settings(
        github_webhook_secret=None,
        github_legacy_secret_token="legacy-token",
        llm_provider="null",
        bot_data_dir=settings.bot_data_dir,
        log_level="WARNING",
    )
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/webhooks/github",
            content=json.dumps(push_payload()).encode(),
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "legacy-1",
                "X-Hub-Token": "legacy-token",
            },
        )
    assert response.status_code == 202
    assert queue.counts()["queued"] == 1


def test_watched_branches_still_enqueue_and_are_filtered_by_the_worker(
    client: TestClient, queue: JobQueue
) -> None:
    """Filtering happens in the analyzer, so the receiver stays trivial."""
    assert post(client, push_payload(ref="refs/heads/main")).status_code == 202
    assert queue.counts()["queued"] == 1
