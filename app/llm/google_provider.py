"""Google Gemini provider (Generative Language API).

Uses the developer API key (``GOOGLE_API_KEY``) against
``generativelanguage.googleapis.com`` with native structured output:
``responseMimeType=application/json`` plus an explicit ``responseSchema``, so
the model returns ChangeSummary-shaped JSON rather than Markdown to parse.

Cost notes: the default model is gemini-2.5-flash and the thinking budget
defaults to 0, because change summarisation is a short, well-scoped task.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.llm.base import BaseLLMProvider, FatalLLMError, RetryableLLMError, parse_summary_json
from app.models import CHANGE_SUMMARY_JSON_SCHEMA, ChangeSummary

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _gemini_schema() -> dict[str, Any]:
    """Gemini accepts an OpenAPI subset: no $defs, no additionalProperties."""
    schema = {
        "type": "object",
        "properties": {
            name: {k: v for k, v in spec.items() if k != "description"}
            | ({"description": spec["description"]} if "description" in spec else {})
            for name, spec in CHANGE_SUMMARY_JSON_SCHEMA["properties"].items()
        },
        "required": list(CHANGE_SUMMARY_JSON_SCHEMA["required"]),
        # Keeps field order stable across calls, which makes responses cheaper
        # to diff when debugging.
        "propertyOrdering": list(CHANGE_SUMMARY_JSON_SCHEMA["properties"]),
    }
    return schema


class GoogleProvider(BaseLLMProvider):
    """Gemini via the Generative Language REST API."""

    name = "google"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        api_base: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 120.0,
        max_retries: int = 2,
        thinking_budget: int | None = 0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(max_retries=max_retries)
        if not api_key:
            raise FatalLLMError("GOOGLE_API_KEY is required for the google provider")
        self._api_key = api_key
        self.model = model
        self._api_base = api_base.rstrip("/")
        self._thinking_budget = thinking_budget
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"user-agent": "git-change-bot/1.0"},
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _build_body(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(),
            "maxOutputTokens": 8192,
        }
        if self._thinking_budget is not None:
            generation["thinkingConfig"] = {"thinkingBudget": self._thinking_budget}
        return {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation,
        }

    async def _complete(self, system_prompt: str, user_prompt: str) -> ChangeSummary:
        url = f"{self._api_base}/models/{self.model}:generateContent"
        started = time.monotonic()
        try:
            response = await self._client.post(
                url,
                json=self._build_body(system_prompt, user_prompt),
                # Header auth keeps the key out of URLs, logs and proxy traces.
                headers={"x-goog-api-key": self._api_key},
            )
        except httpx.TimeoutException as exc:
            raise RetryableLLMError(f"gemini request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RetryableLLMError(f"gemini request failed: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise RetryableLLMError(f"gemini returned {response.status_code}")
        if response.status_code >= 400:
            detail = response.text[:500]
            raise FatalLLMError(f"gemini returned {response.status_code}: {detail}")

        latency_ms = int((time.monotonic() - started) * 1000)
        payload = response.json()
        text = self._extract_text(payload)
        usage = payload.get("usageMetadata") or {}
        log.info(
            "llm response",
            extra={
                "provider": self.name,
                "model": self.model,
                "latency_ms": latency_ms,
                "prompt_tokens": usage.get("promptTokenCount"),
                "output_tokens": usage.get("candidatesTokenCount"),
                "total_tokens": usage.get("totalTokenCount"),
            },
        )
        return parse_summary_json(text)

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        if feedback := payload.get("promptFeedback", {}).get("blockReason"):
            raise FatalLLMError(f"gemini blocked the prompt: {feedback}")
        candidates = payload.get("candidates") or []
        if not candidates:
            raise RetryableLLMError("gemini returned no candidates")
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text.strip():
            if finish == "MAX_TOKENS":
                raise RetryableLLMError("gemini hit the output token limit before answering")
            raise RetryableLLMError(f"gemini returned empty content (finishReason={finish})")
        return text
