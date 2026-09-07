"""Slack Incoming Webhook notifications.

Block Kit is built here and nowhere else, so message shape stays consistent
across the analysed, lightweight and failure paths. The webhook URL is a
secret: it is never logged, and never included in an error message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.errors import SlackError
from app.models import ChangeSummary, DiffResult, PushEvent

log = logging.getLogger(__name__)

# Slack rejects blocks over these sizes with an unhelpful error, so clamp.
MAX_TEXT_BLOCK = 2900
MAX_HEADER = 140
MAX_BLOCKS = 45

RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _escape_mrkdwn(text: str) -> str:
    """Escape Slack control characters in untrusted text.

    Slack interprets ``&``, ``<`` and ``>`` inside mrkdwn text objects. The
    repository and model supply most message content, so escape those values
    before adding our own formatting.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_code(text: str, limit: int = 100) -> str:
    """Render a bounded value as inline code without allowing a stray backtick."""
    value = _escape_mrkdwn(_truncate(text, limit)).replace("`", "'")
    return f"`{value}`"


def _bullets(items: list[str], *, limit: int = 8) -> str:
    if not items:
        return ""
    shown = [f"• {_escape_mrkdwn(_truncate(item, 400))}" for item in items[:limit]]
    if len(items) > limit:
        shown.append(f"• …and {len(items) - limit} more")
    return _truncate("\n".join(shown), MAX_TEXT_BLOCK)


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text, MAX_TEXT_BLOCK)}}


def _titled(title: str, body: str) -> list[dict[str, Any]]:
    return [_section(f"*{title}*\n{body}")] if body.strip() else []


def compare_link(event: PushEvent, diff: DiffResult | None = None) -> str | None:
    """Best link to the change: GitHub's compare view, else the commit page."""
    if event.compare_url:
        return event.compare_url
    if not event.web_url:
        return None
    base = event.web_url.rstrip("/")
    baseline = (diff.baseline_sha if diff else "") or event.before_sha
    if baseline and baseline != "0" * 40:
        return f"{base}/compare/{baseline}...{event.after_sha}"
    return f"{base}/commit/{event.after_sha}"


def build_change_blocks(
    event: PushEvent, summary: ChangeSummary, diff: DiffResult
) -> list[dict[str, Any]]:
    """Full Block Kit message for an analysed push."""
    risk = summary.risk.lower()
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _truncate(
                    f"{RISK_EMOJI.get(risk, '⚪')} Code change · {event.project_name}",
                    MAX_HEADER,
                ),
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Branch*\n{_inline_code(event.branch)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Risk level*\n{RISK_EMOJI.get(risk, '⚪')} {risk.title()}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Author*\n{_escape_mrkdwn(_truncate(event.author_name, 100))}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Change size*\n`+{diff.total_additions}` additions · "
                    f"`−{diff.total_deletions}` deletions",
                },
            ],
        },
        {"type": "divider"},
        _section(f"*Summary*\n{_escape_mrkdwn(summary.summary)}"),
    ]

    blocks += _titled("Key changes", _bullets(summary.changes))
    blocks += _titled("Affected areas", _bullets(summary.affected_components, limit=6))
    blocks.append({"type": "divider"})

    file_count = diff.total_files or len(diff.files) + len(diff.ignored_files) + len(
        diff.dropped_files
    )
    context_bits = [
        f"{file_count} file{'s' if file_count != 1 else ''}",
        f"{event.commit_count} commit{'s' if event.commit_count != 1 else ''}",
        _inline_code(f"{event.before_sha[:8]} → {event.after_sha[:8]}", 24),
    ]
    if diff.ignored_files:
        context_bits.append(f"{len(diff.ignored_files)} generated/binary skipped")
    if diff.dropped_files:
        context_bits.append(f"{len(diff.dropped_files)} omitted for size")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(context_bits)}],
        }
    )

    if link := compare_link(event, diff):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Review diff on GitHub",
                            "emoji": True,
                        },
                        "url": link,
                    }
                ],
            }
        )
    return blocks[:MAX_BLOCKS]


def build_simple_blocks(
    event: PushEvent, title: str, detail: str, *, link: str | None = None
) -> list[dict[str, Any]]:
    """Cheap deterministic message: branch created/deleted, ignored-only push."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _truncate(f"GitHub update · {event.project_name}", MAX_HEADER),
                "emoji": True,
            },
        },
        _section(f"*{_escape_mrkdwn(title)}*\n{_escape_mrkdwn(detail)}"),
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{_inline_code(event.branch)} · "
                    f"{_escape_mrkdwn(event.author_name)} · No AI analysis needed",
                }
            ],
        },
    ]
    if link:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Open on GitHub",
                            "emoji": True,
                        },
                        "url": link,
                    }
                ],
            }
        )
    return blocks


def fallback_text(event: PushEvent, summary: ChangeSummary | None = None) -> str:
    """Plain-text fallback used for notifications and accessibility."""
    if summary is None:
        return _escape_mrkdwn(f"Code change in {event.project_name} on {event.branch}")
    return _truncate(
        _escape_mrkdwn(
            f"[{summary.risk.upper()}] {event.project_name}/{event.branch}: {summary.summary}"
        ),
        400,
    )


class SlackNotifier:
    """Posts Block Kit messages to an Incoming Webhook, with bounded retries."""

    def __init__(
        self,
        webhook_url: str | None,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers={"user-agent": "git-change-bot/1.0"},
        )
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, *, text: str, blocks: list[dict[str, Any]]) -> None:
        """Deliver one message. Raises SlackError once retries are exhausted."""
        if not self._webhook_url:
            log.warning("slack webhook not configured; notification skipped")
            return

        payload = {"text": text, "blocks": blocks}
        last_error = "unknown error"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._webhook_url, json=payload)
            except httpx.HTTPError as exc:
                # str(exc) can contain the webhook URL — report the type only.
                last_error = f"transport error ({type(exc).__name__})"
            else:
                if response.status_code < 300:
                    log.info("slack notified", extra={"status": response.status_code})
                    return
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code < 500 and response.status_code != 429:
                    break  # malformed payload: retrying will not help
            if attempt < self._max_retries:
                await asyncio.sleep(2**attempt)
        raise SlackError(f"slack delivery failed: {last_error}")

    async def send_change(self, event: PushEvent, summary: ChangeSummary, diff: DiffResult) -> None:
        await self.send(
            text=fallback_text(event, summary),
            blocks=build_change_blocks(event, summary, diff),
        )

    async def send_simple(
        self, event: PushEvent, title: str, detail: str, *, link: str | None = None
    ) -> None:
        await self.send(
            text=f"{title} · {event.project_name} ({event.branch})",
            blocks=build_simple_blocks(event, title, detail, link=link),
        )
