"""LLM provider registry.

``build_provider`` is the only place that knows which concrete backend is in
use; everything else depends on the ``LLMProvider`` protocol.
"""

from __future__ import annotations

from app.errors import ConfigError
from app.llm.base import (
    SYSTEM_PROMPT,
    BaseLLMProvider,
    LLMProvider,
    build_user_prompt,
    parse_summary_json,
)
from app.llm.google_provider import GoogleProvider
from app.llm.null_provider import NullProvider
from app.settings import Settings

__all__ = [
    "SYSTEM_PROMPT",
    "BaseLLMProvider",
    "GoogleProvider",
    "LLMProvider",
    "NullProvider",
    "build_provider",
    "build_user_prompt",
    "parse_summary_json",
]


def build_provider(settings: Settings) -> LLMProvider:
    """Instantiate the configured provider."""
    match settings.llm_provider:
        case "google":
            if settings.google_api_key is None:
                raise ConfigError("GOOGLE_API_KEY is not set")
            return GoogleProvider(
                api_key=settings.google_api_key.get_secret_value(),
                model=settings.google_model,
                api_base=settings.google_api_base,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                thinking_budget=settings.google_thinking_budget,
            )
        case "null":
            return NullProvider()
        case unknown:  # pragma: no cover - guarded by Settings validation
            raise ConfigError(f"unknown LLM_PROVIDER: {unknown}")
