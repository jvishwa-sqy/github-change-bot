"""Slack Block Kit construction and delivery."""

from __future__ import annotations

import json

import httpx
import pytest

from app.errors import SlackError
from app.models import ChangeSummary, DiffResult, PushEvent
from app.slack import SlackNotifier, build_change_blocks, build_simple_blocks, compare_link

WEBHOOK = "https://hooks.slack.com/services/T0/B0/secret-token"


def event(**overrides) -> PushEvent:
    base = {
        "project_id": 1,
        "project_name": "acme/ai-caller-core",
        "repo_url": "git@github.com:acme/ai-caller-core.git",
        "web_url": "https://github.com/acme/ai-caller-core",
        "ref": "refs/heads/feature/dotcom-fix",
        "before_sha": "a" * 40,
        "after_sha": "b" * 40,
        "author_name": "Vishwa",
        "commit_count": 2,
    }
    base.update(overrides)
    return PushEvent(**base)


def summary() -> ChangeSummary:
    return ChangeSummary(
        summary="Enabled language switching for Dotcom inbound calls.",
        changes=["DOTCOM now registers existing language tools.", "Cold calling unchanged."],
        affected_components=["Dotcom inbound listener", "Language tool registration"],
        impact=["Dotcom callers can dynamically switch language."],
        risk="medium",
        risk_reason="Runtime tool initialization on the live call path changed.",
        recommended_tests=["Test English → Hindi → English.", "Verify cold-calling behaviour."],
    )


def diff() -> DiffResult:
    return DiffResult(
        total_additions=28, total_deletions=9, baseline_sha="a" * 40, ignored_files=["yarn.lock"]
    )


def test_change_blocks_contain_every_section() -> None:
    blocks = build_change_blocks(event(), summary(), diff())
    rendered = json.dumps(blocks, ensure_ascii=False)

    assert blocks[0]["type"] == "header"
    assert "Code Change · acme/ai-caller-core" in blocks[0]["text"]["text"]
    assert "feature/dotcom-fix" in rendered
    assert "Vishwa" in rendered
    assert "+28 −9" in rendered
    assert "MEDIUM" in rendered
    for section in (
        "Summary",
        "What changed",
        "Affected",
        "Impact",
        "Risk reason",
        "Recommended tests",
    ):
        assert f"*{section}*" in rendered or section in rendered
    assert "View Diff" in rendered


def test_button_links_to_the_compare_page() -> None:
    link = compare_link(event(), diff())
    assert link == f"https://github.com/acme/ai-caller-core/compare/{'a' * 40}...{'b' * 40}"


def test_compare_url_from_payload_wins() -> None:
    supplied = "https://github.com/acme/app/compare/aaa...bbb"
    assert compare_link(event(compare_url=supplied), diff()) == supplied


def test_link_falls_back_to_commit_page_for_new_branches() -> None:
    link = compare_link(event(before_sha="0" * 40), DiffResult(baseline_sha=""))
    assert link.endswith(f"/commit/{'b' * 40}")


def test_blocks_are_truncated_to_slack_limits() -> None:
    huge = summary()
    huge.summary = "x" * 10_000
    huge.changes = [f"change {i}" for i in range(50)]
    blocks = build_change_blocks(event(), huge, diff())
    for block in blocks:
        if block.get("type") == "section" and "text" in block:
            assert len(block["text"]["text"]) <= 2900
    assert "and 42 more" in json.dumps(blocks)


def test_simple_blocks_state_no_llm_was_used() -> None:
    blocks = build_simple_blocks(event(), "Branch deleted", "`x` was deleted.")
    assert "no LLM analysis needed" in json.dumps(blocks)


async def test_notifier_posts_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        notifier = SlackNotifier(WEBHOOK, client=http)
        await notifier.send_change(event(), summary(), diff())

    assert seen["url"] == WEBHOOK
    assert "blocks" in seen["body"]
    assert seen["body"]["text"].startswith("[MEDIUM]")


async def test_notifier_retries_server_errors() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500 if calls["n"] == 1 else 200, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await SlackNotifier(WEBHOOK, client=http, max_retries=1).send(text="t", blocks=[])
    assert calls["n"] == 2


async def test_notifier_does_not_retry_bad_payloads() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="invalid_blocks")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SlackError):
            await SlackNotifier(WEBHOOK, client=http, max_retries=2).send(text="t", blocks=[])
    assert calls["n"] == 1


async def test_transport_errors_never_leak_the_webhook_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SlackError) as error:
            await SlackNotifier(WEBHOOK, client=http, max_retries=0).send(text="t", blocks=[])
    assert "secret-token" not in str(error.value)


async def test_notifier_without_url_is_disabled() -> None:
    notifier = SlackNotifier(None)
    assert not notifier.enabled
    await notifier.send(text="t", blocks=[])  # no exception, no request
    await notifier.aclose()
