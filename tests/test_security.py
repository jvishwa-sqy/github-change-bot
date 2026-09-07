"""Webhook authentication tests."""

from __future__ import annotations

import time

import pytest

from app.errors import WebhookVerificationError
from app.security import compute_signature, verify_webhook

SECRET = "s3cret-signing-token"
BODY = b'{"ref":"refs/heads/main","after":"abc"}'


def headers(**overrides: str) -> dict[str, str]:
    base = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "11111111-2222-3333-4444-555555555555",
        "X-Hub-Signature-256": compute_signature(SECRET, BODY),
    }
    base.update(overrides)
    return base


def test_valid_signature_is_accepted() -> None:
    result = verify_webhook(headers=headers(), body=BODY, signing_secret=SECRET)
    assert result.method == "hmac-sha256"
    assert result.event == "push"
    assert result.delivery_id == "11111111-2222-3333-4444-555555555555"


def test_signature_is_case_and_header_name_insensitive() -> None:
    raw = {"x-hub-signature-256": compute_signature(SECRET, BODY), "X-GITHUB-EVENT": "push"}
    assert verify_webhook(headers=raw, body=BODY, signing_secret=SECRET).event == "push"


def test_invalid_signature_is_rejected() -> None:
    bad = headers(**{"X-Hub-Signature-256": compute_signature("wrong-secret", BODY)})
    with pytest.raises(WebhookVerificationError):
        verify_webhook(headers=bad, body=BODY, signing_secret=SECRET)


def test_tampered_body_is_rejected() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(headers=headers(), body=BODY + b" ", signing_secret=SECRET)


def test_malformed_signature_header_is_rejected() -> None:
    with pytest.raises(WebhookVerificationError, match="malformed"):
        verify_webhook(
            headers=headers(**{"X-Hub-Signature-256": "sha256=not-hex!!"}),
            body=BODY,
            signing_secret=SECRET,
        )


def test_missing_signature_is_rejected_when_required() -> None:
    bare = {"X-GitHub-Event": "push"}
    with pytest.raises(WebhookVerificationError):
        verify_webhook(headers=bare, body=BODY, signing_secret=SECRET)


def test_unsigned_allowed_when_not_required() -> None:
    result = verify_webhook(
        headers={"X-GitHub-Event": "push"},
        body=BODY,
        signing_secret=None,
        require_signature=False,
    )
    assert result.method == "unverified"


def test_expired_timestamp_is_rejected() -> None:
    stale = headers(**{"X-Hub-Timestamp": str(int(time.time()) - 3600)})
    with pytest.raises(WebhookVerificationError, match="window"):
        verify_webhook(headers=stale, body=BODY, signing_secret=SECRET, max_age_seconds=300)


def test_future_timestamp_is_rejected() -> None:
    future = headers(**{"X-Hub-Timestamp": str(int(time.time()) + 3600)})
    with pytest.raises(WebhookVerificationError, match="window"):
        verify_webhook(headers=future, body=BODY, signing_secret=SECRET, max_age_seconds=300)


def test_fresh_timestamp_is_accepted() -> None:
    fresh = headers(**{"X-Hub-Timestamp": str(int(time.time()) - 10)})
    assert verify_webhook(headers=fresh, body=BODY, signing_secret=SECRET).method == "hmac-sha256"


def test_legacy_token_is_accepted() -> None:
    result = verify_webhook(
        headers={"X-Hub-Token": "legacy-token", "X-GitHub-Event": "push"},
        body=BODY,
        signing_secret=None,
        legacy_token="legacy-token",
    )
    assert result.method == "legacy-token"


def test_legacy_token_mismatch_is_rejected() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(
            headers={"X-Gitlab-Token": "nope", "X-GitHub-Event": "push"},
            body=BODY,
            signing_secret=None,
            legacy_token="legacy-token",
        )


def test_signature_takes_precedence_over_token() -> None:
    both = headers(**{"X-Hub-Token": "wrong-token"})
    assert (
        verify_webhook(
            headers=both, body=BODY, signing_secret=SECRET, legacy_token="legacy-token"
        ).method
        == "hmac-sha256"
    )
