"""FastAPI webhook receiver.

The endpoint does the minimum: verify, parse, enqueue, return 202. All git and
LLM work happens in the separate worker process, so GitHub never waits on it
and a slow analysis can never cause a webhook retry storm.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app.errors import WebhookVerificationError
from app.git_repo import short
from app.logging_setup import configure_logging
from app.models import PushEvent, PushEventKind
from app.queue import JobQueue
from app.security import verify_webhook
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 25 * 1024 * 1024  # GitHub caps payloads at 25 MB


def get_queue(request: Request) -> JobQueue:
    """Dependency: the process-wide queue handle created at startup."""
    return request.app.state.queue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, json_output=settings.log_json)
    settings.ensure_directories()
    app.state.queue = JobQueue(
        settings.db_path,
        max_attempts=settings.max_job_attempts,
        lease_seconds=settings.job_lease_seconds,
        retry_backoff_seconds=settings.retry_backoff_seconds,
    )
    log.info(
        "webhook receiver ready",
        extra={"data_dir": str(settings.bot_data_dir), "provider": settings.llm_provider},
    )
    try:
        yield
    finally:
        app.state.queue.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory — lets tests inject settings without env juggling."""
    app = FastAPI(
        title="git-change-bot",
        version="1.0.0",
        description="GitHub push webhook receiver for incremental code-change analysis.",
        lifespan=lifespan,
        docs_url=None,  # nothing to expose publicly
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings or get_settings()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(queue: Annotated[JobQueue, Depends(get_queue)]) -> dict[str, Any]:
        """Liveness plus queue depth — handy for monitoring."""
        return {"status": "ok", "queue": queue.counts()}

    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(
        request: Request,
        queue: Annotated[JobQueue, Depends(get_queue)],
    ) -> Response:
        cfg: Settings = request.app.state.settings
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(
                {"status": "rejected", "reason": "payload too large"},
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        try:
            verified = verify_webhook(
                headers=request.headers,
                body=body,
                signing_secret=(
                    cfg.github_webhook_secret.get_secret_value()
                    if cfg.github_webhook_secret
                    else None
                ),
                legacy_token=(
                    cfg.github_legacy_secret_token.get_secret_value()
                    if cfg.github_legacy_secret_token
                    else None
                ),
                max_age_seconds=cfg.webhook_max_age_seconds,
                require_signature=cfg.require_webhook_signature,
            )
        except WebhookVerificationError as exc:
            # Log the reason but tell the caller nothing beyond "unauthorized".
            log.warning("webhook rejected", extra={"reason": str(exc)})
            return JSONResponse(
                {"status": "unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED
            )

        if verified.event == "ping":
            return JSONResponse({"status": "pong"}, status_code=status.HTTP_200_OK)
        if verified.event != "push":
            return JSONResponse(
                {"status": "ignored", "reason": f"unsupported event: {verified.event or 'none'}"},
                status_code=status.HTTP_202_ACCEPTED,
            )

        # A repeated delivery id is a replay (or GitHub's own retry); the
        # queue's dedupe key covers the rest.
        if verified.delivery_id and not queue.record_delivery(verified.delivery_id):
            log.info("duplicate delivery ignored", extra={"delivery": verified.delivery_id})
            return JSONResponse({"status": "duplicate"}, status_code=status.HTTP_202_ACCEPTED)

        try:
            payload = json.loads(body)
            event = PushEvent.from_github_payload(payload)
        except (ValueError, TypeError) as exc:
            log.warning("malformed push payload", extra={"error": str(exc)[:200]})
            return JSONResponse(
                {"status": "rejected", "reason": "malformed payload"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if not event.is_branch_ref:
            return JSONResponse(
                {"status": "ignored", "reason": "not a branch ref"},
                status_code=status.HTTP_202_ACCEPTED,
            )
        if event.kind is PushEventKind.NORMAL and event.commit_count == 0:
            # A no-op push (e.g. re-push of the same head) is not worth a job.
            return JSONResponse(
                {"status": "ignored", "reason": "no commits"},
                status_code=status.HTTP_202_ACCEPTED,
            )

        job, created = queue.enqueue(event)
        log.info(
            "push enqueued" if created else "push already queued",
            extra={
                "job_id": job.id,
                "project_id": event.project_id,
                "project": event.project_name,
                "branch": event.branch,
                "before": short(event.before_sha),
                "after": short(event.after_sha),
                "commits": event.commit_count,
                "auth": verified.method,
            },
        )
        return JSONResponse(
            {"status": "accepted" if created else "duplicate", "job_id": job.id},
            status_code=status.HTTP_202_ACCEPTED,
        )

    return app


app = create_app()
