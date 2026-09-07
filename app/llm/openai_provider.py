"""OpenAI provider using the Responses API with structured outputs.

Kept as a second implementation so the provider abstraction is exercised, not
just declared. Select it with ``LLM_PROVIDER=openai``.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

import httpx

from app.llm.base import BaseLLMProvider, FatalLLMError, RetryableLLMError, parse_summary_json
from app.models import CHANGE_SUMMARY_JSON_SCHEMA, ChangeSummary

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _strict_schema() -> dict[str, Any]:
    """Strict mode requires additionalProperties:false and all keys required."""
    schema = copy.deepcopy(CHANGE_SUMMARY_JSON_SCHEMA)
    schema["additionalProperties"] = False
    schema["required"] = list(schema["properties"])
    return schema


class OpenAIProvider(BaseLLMProvider):
    """OpenAI via POST /v1/responses (not the deprecated completions APIs)."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5",
        api_base: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(max_retries=max_retries)
        if not api_key:
            raise FatalLLMError("OPENAI_API_KEY is required for the openai provider")
        self._api_key = api_key
        self.model = model
        self._api_base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"user-agent": "git-change-bot/1.0"},
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _complete(self, system_prompt: str, user_prompt: str) -> ChangeSummary:
        body: dict[str, Any] = {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "change_summary",
                    "strict": True,
                    "schema": _strict_schema(),
                }
            },
        }
        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self._api_base}/responses",
                json=body,
                headers={"authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise RetryableLLMError(f"openai request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RetryableLLMError(f"openai request failed: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise RetryableLLMError(f"openai returned {response.status_code}")
        if response.status_code >= 400:
            raise FatalLLMError(f"openai returned {response.status_code}: {response.text[:500]}")

        payload = response.json()
        usage = payload.get("usage") or {}
        log.info(
            "llm response",
            extra={
                "provider": self.name,
                "model": self.model,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "prompt_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
        )
        return parse_summary_json(self._extract_text(payload))

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"]
        chunks: list[str] = []
        for item in payload.get("output") or []:
            if item.get("type") == "refusal" or item.get("role") == "assistant":
                for content in item.get("content") or []:
                    if content.get("type") == "refusal":
                        raise FatalLLMError(f"openai refused: {content.get('refusal', '')[:200]}")
                    if text := content.get("text"):
                        chunks.append(text)
        joined = "".join(chunks)
        if not joined.strip():
            status = payload.get("status")
            raise RetryableLLMError(f"openai returned empty output (status={status})")
        return joined
