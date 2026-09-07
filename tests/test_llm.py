"""LLM abstraction, structured output and provider transport behaviour."""

from __future__ import annotations

import json

import httpx
import pytest

from app.errors import LLMResponseError
from app.llm import build_provider
from app.llm.base import (
    BaseLLMProvider,
    FatalLLMError,
    RetryableLLMError,
    build_synthesis_prompt,
    build_user_prompt,
    parse_summary_json,
)
from app.llm.google_provider import GoogleProvider, _gemini_schema
from app.models import (
    CHANGE_SUMMARY_JSON_SCHEMA,
    ChangeAnalysisRequest,
    ChangeStatus,
    ChangeSummary,
    FileContext,
)

VALID_SUMMARY = {
    "summary": "Dotcom inbound calls can now register language-switching tools.",
    "changes": ["DOTCOM registers the existing language tools."],
    "affected_components": ["Dotcom inbound listener"],
    "risk": "medium",
}


def make_request(**overrides) -> ChangeAnalysisRequest:
    base = ChangeAnalysisRequest(
        project_name="acme/ai-caller-core",
        branch="feature/dotcom-fix",
        author="Vishwa",
        before_sha="a" * 40,
        after_sha="b" * 40,
        commit_messages=["Enable language tools for dotcom"],
        stat_text=" app/listener.py | 4 +++-",
        total_additions=28,
        total_deletions=9,
        files=[
            FileContext(
                path="app/listener.py",
                language="python",
                status=ChangeStatus.MODIFIED,
                additions=28,
                deletions=9,
                diff="@@ -1 +1 @@\n-old\n+new",
                code_context="   1| def register_tools(): ...",
                context_kind="symbol",
                symbols=["DotcomListener.register_tools"],
                imports=["app.language_tools"],
            )
        ],
        repo_overview="Repository map …",
    )
    return base.model_copy(update=overrides)


# ------------------------------------------------------------------- schema
def test_schema_matches_the_pydantic_model() -> None:
    """The hand-written provider schema must not drift from ChangeSummary."""
    assert set(CHANGE_SUMMARY_JSON_SCHEMA["properties"]) == set(ChangeSummary.model_fields)
    assert set(CHANGE_SUMMARY_JSON_SCHEMA["required"]) == set(ChangeSummary.model_fields)


def test_gemini_schema_has_no_unsupported_keywords() -> None:
    schema = _gemini_schema()
    serialised = json.dumps(schema)
    assert "$defs" not in serialised
    assert "additionalProperties" not in serialised
    assert schema["propertyOrdering"][0] == "summary"


# ------------------------------------------------------------------ parsing
def test_parse_valid_json() -> None:
    summary = parse_summary_json(json.dumps(VALID_SUMMARY))
    assert summary.risk == "medium"
    assert summary.changes[0].startswith("DOTCOM")


def test_parse_json_wrapped_in_a_code_fence() -> None:
    fenced = "```json\n" + json.dumps(VALID_SUMMARY) + "\n```"
    assert parse_summary_json(fenced).summary == VALID_SUMMARY["summary"]


def test_parse_rejects_markdown() -> None:
    with pytest.raises(LLMResponseError):
        parse_summary_json("## Summary\nSome prose about the change.")


def test_parse_rejects_wrong_shape() -> None:
    with pytest.raises(LLMResponseError):
        parse_summary_json(json.dumps({"summary": "x", "risk": "catastrophic"}))


# ------------------------------------------------------------------ prompts
def test_user_prompt_contains_diff_and_context_but_not_whole_repo() -> None:
    prompt = build_user_prompt(make_request())
    assert "acme/ai-caller-core" in prompt
    assert "feature/dotcom-fix" in prompt
    assert "app/listener.py" in prompt
    assert "```diff" in prompt
    assert "Enclosing definitions" in prompt
    assert "DotcomListener.register_tools" in prompt


def test_synthesis_prompt_lists_subsystems() -> None:
    summary = ChangeSummary(**VALID_SUMMARY)
    prompt = build_synthesis_prompt(make_request(), [("app/routing", summary), ("tests", summary)])
    assert "app/routing" in prompt and "tests" in prompt


# ---------------------------------------------------------------- providers
def gemini_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 200},
        },
    )


async def test_google_provider_sends_structured_request() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return gemini_response(VALID_SUMMARY)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        provider = GoogleProvider(api_key="k", model="gemini-2.5-flash", client=http)
        summary = await provider.summarize_change(make_request())

    assert summary.risk == "medium"
    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")
    # The key travels in a header, never in the URL.
    assert "key=" not in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "k"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"]["generationConfig"]["responseSchema"]["type"] == "object"
    assert seen["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
    assert "senior software engineer" in seen["body"]["systemInstruction"]["parts"][0]["text"]


async def test_google_provider_retries_transient_failures() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="overloaded")
        return gemini_response(VALID_SUMMARY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = GoogleProvider(api_key="k", client=http, max_retries=2)
        summary = await provider.summarize_change(make_request())

    assert calls["n"] == 2
    assert summary.summary


async def test_google_provider_does_not_retry_client_errors() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad api key")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = GoogleProvider(api_key="k", client=http, max_retries=2)
        with pytest.raises(FatalLLMError):
            await provider.summarize_change(make_request())

    assert calls["n"] == 1


async def test_google_provider_reports_blocked_prompts() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = GoogleProvider(api_key="k", client=http, max_retries=0)
        with pytest.raises(FatalLLMError, match="blocked"):
            await provider.summarize_change(make_request())


async def test_google_provider_handles_truncated_output() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = GoogleProvider(api_key="k", client=http, max_retries=0)
        with pytest.raises(RetryableLLMError, match="token limit"):
            await provider.summarize_change(make_request())


async def test_null_provider_makes_no_calls(settings) -> None:
    provider = build_provider(settings)
    assert provider.name == "null"
    summary = await provider.summarize_change(make_request())
    assert "disabled" in summary.summary
    assert summary.risk == "low"


def test_build_provider_selects_google(settings) -> None:
    configured = type(settings)(
        github_webhook_secret="x",
        llm_provider="google",
        google_api_key="test-key",
        bot_data_dir=settings.bot_data_dir,
    )
    provider = build_provider(configured)
    assert isinstance(provider, GoogleProvider)
    assert provider.model == "gemini-2.5-flash"


def test_provider_interface_is_satisfied() -> None:
    assert issubclass(GoogleProvider, BaseLLMProvider)
