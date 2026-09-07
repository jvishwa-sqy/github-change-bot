"""GitHub webhook authentication.

GitHub signs the raw request body with HMAC-SHA256 using the secret configured
on the webhook, and sends it as:

    X-Hub-Signature-256: sha256=<hex digest>

Every comparison here is constant-time, and no secret or signature value is
ever logged. Replay protection has two layers:

  * the delivery id (``X-GitHub-Delivery``) is recorded and refused twice —
    this is the guard that actually applies to GitHub, which sends no timestamp;
  * if a fronting proxy adds a timestamp header, deliveries older than
    ``webhook_max_age_seconds`` (~5 min) are refused as well.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from app.errors import WebhookVerificationError

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-hub-signature-256"
LEGACY_TOKEN_HEADER = "x-hub-token"
GITLAB_TOKEN_HEADER = "x-gitlab-token"  # accepted for forwarders/compatibility
EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"
# Optional; only present when something in front of us adds it.
TIMESTAMP_HEADERS = ("x-hub-timestamp", "x-webhook-timestamp", "x-request-timestamp")


@dataclass(frozen=True)
class VerifiedWebhook:
    """Result of a successful verification."""

    event: str
    delivery_id: str
    method: str  # "hmac-sha256" | "legacy-token" | "unverified"


def compute_signature(secret: str, body: bytes) -> str:
    """Return the ``sha256=…`` header value GitHub would send for this body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _headers_lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _check_timestamp(raw: str, max_age_seconds: int) -> None:
    """Reject stale deliveries. Accepts epoch seconds or an HTTP date."""
    value = raw.strip()
    try:
        sent_at = float(value)
    except ValueError:
        try:
            sent_at = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError) as exc:
            raise WebhookVerificationError("unparseable webhook timestamp") from exc
    age = time.time() - sent_at
    # A small negative age is normal clock skew; a large one is a forged future
    # timestamp trying to buy an unlimited replay window.
    if age > max_age_seconds or age < -max_age_seconds:
        raise WebhookVerificationError("webhook timestamp outside the accepted window")


def verify_webhook(
    *,
    headers: Mapping[str, str],
    body: bytes,
    signing_secret: str | None,
    legacy_token: str | None = None,
    max_age_seconds: int = 300,
    require_signature: bool = True,
) -> VerifiedWebhook:
    """Authenticate an inbound webhook request.

    Raises WebhookVerificationError on any failure. The caller maps that to
    HTTP 401 and must not reveal which check failed.
    """
    lower = _headers_lower(headers)
    event = lower.get(EVENT_HEADER, "").strip()
    delivery_id = lower.get(DELIVERY_HEADER, "").strip()

    for header in TIMESTAMP_HEADERS:
        if header in lower:
            _check_timestamp(lower[header], max_age_seconds)
            break

    signature = lower.get(SIGNATURE_HEADER, "").strip()
    if signing_secret and signature:
        expected = compute_signature(signing_secret, body)
        # compare_digest on str requires ASCII-only, which hex digests are;
        # a malformed header could contain anything, so normalise first.
        if not signature.startswith("sha256=") or not signature[7:].isalnum():
            raise WebhookVerificationError("malformed signature header")
        if not hmac.compare_digest(signature, expected):
            raise WebhookVerificationError("signature mismatch")
        return VerifiedWebhook(event=event, delivery_id=delivery_id, method="hmac-sha256")

    token = lower.get(LEGACY_TOKEN_HEADER) or lower.get(GITLAB_TOKEN_HEADER)
    if legacy_token and token:
        if not hmac.compare_digest(token.strip(), legacy_token):
            raise WebhookVerificationError("token mismatch")
        return VerifiedWebhook(event=event, delivery_id=delivery_id, method="legacy-token")

    if require_signature:
        raise WebhookVerificationError("request is not authenticated")

    log.warning("accepting unauthenticated webhook (REQUIRE_WEBHOOK_SIGNATURE=false)")
    return VerifiedWebhook(event=event, delivery_id=delivery_id, method="unverified")
