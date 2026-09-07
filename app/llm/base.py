"""LLM provider interface and prompt construction.

The rest of the application only ever sees ``LLMProvider``; swapping Google for
OpenAI, Anthropic, Azure or a self-hosted model means adding one subclass that
implements ``_complete`` and registering it in ``app/llm/__init__.py``.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import random
from typing import Any, Protocol, runtime_checkable

from app.errors import LLMError, LLMResponseError
from app.models import ChangeAnalysisRequest, ChangeSummary

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior software engineer analyzing a Git change.

Analyze only the supplied diff and repository context.

Explain behavioral and architectural changes, not superficial syntax changes.

Do not invent behavior that cannot be inferred from the provided code.

Prefer:
"Dotcom inbound calls can now register language-switching tools."

Avoid:
"Added an if condition in listener.py."

Identify:
- overall change
- concrete behavioral changes
- affected components
- runtime/product impact
- realistic risks
- tests that should be executed

Secrets in the supplied text have been replaced with [REDACTED]; treat those as
opaque and never speculate about their values.

If context is insufficient, state that clearly rather than guessing.
Answer strictly in the required JSON schema."""

SYNTHESIS_PROMPT = """You are a senior software engineer writing the final summary of a
large Git change.

You are given per-subsystem summaries that were produced from the actual diff.
Combine them into one coherent picture of the change as a whole.

Do not invent behavior that is not present in the supplied summaries.
Prefer architectural and behavioral framing over file-by-file narration.
Answer strictly in the required JSON schema."""

_STATUS_LABEL = {
    "added": "added",
    "modified": "modified",
    "deleted": "deleted",
    "renamed": "renamed",
    "copied": "copied",
    "type_changed": "type changed",
}


def build_user_prompt(request: ChangeAnalysisRequest) -> str:
    """Render the bounded analysis request as prompt text."""
    parts: list[str] = []
    scope = f" (subsystem: {request.group_name})" if request.group_name else ""
    parts.append(
        f"Repository: {request.project_name}{scope}\n"
        f"Branch: {request.branch}\n"
        f"Author: {request.author}\n"
        f"Commits: {request.before_sha[:8]} → {request.after_sha[:8]}\n"
        f"Totals: +{request.total_additions} -{request.total_deletions}"
    )

    if request.commit_messages:
        joined = "\n".join(f"- {m.splitlines()[0][:200]}" for m in request.commit_messages)
        parts.append(f"Commit messages:\n{joined}")

    if request.stat_text:
        parts.append(f"Diffstat:\n{request.stat_text[:4000]}")

    if request.repo_overview:
        parts.append(request.repo_overview)

    for context in request.files:
        section = [
            f"### File: {context.path} "
            f"({_STATUS_LABEL.get(context.status.value, context.status.value)}, "
            f"{context.language}, +{context.additions} -{context.deletions})"
        ]
        if context.symbols:
            section.append(f"Symbols in file: {', '.join(context.symbols)}")
        if context.imports:
            section.append(f"Imports: {', '.join(context.imports)}")
        if context.related_files:
            section.append(f"Imported by: {', '.join(context.related_files)}")
        section.append(f"Diff:\n```diff\n{context.diff}\n```")
        if context.code_context:
            label = (
                "Enclosing definitions from the updated file"
                if context.context_kind == "symbol"
                else "Surrounding code from the updated file"
            )
            section.append(f"{label}:\n```\n{context.code_context}\n```")
        parts.append("\n".join(section))

    if request.notes:
        parts.append("Notes:\n" + "\n".join(f"- {n}" for n in request.notes))

    return "\n\n".join(parts)


def build_synthesis_prompt(
    request: ChangeAnalysisRequest, summaries: list[tuple[str, ChangeSummary]]
) -> str:
    """Prompt for the final pass of a hierarchical analysis."""
    parts = [
        f"Repository: {request.project_name}\n"
        f"Branch: {request.branch}\n"
        f"Author: {request.author}\n"
        f"Totals: +{request.total_additions} -{request.total_deletions} "
        f"across {len(summaries)} subsystems"
    ]
    if request.stat_text:
        parts.append(f"Diffstat:\n{request.stat_text[:3000]}")
    for name, summary in summaries:
        parts.append(
            f"### Subsystem: {name}\n"
            f"Summary: {summary.summary}\n"
            f"Changes: {'; '.join(summary.changes) or 'n/a'}\n"
            f"Affected: {'; '.join(summary.affected_components) or 'n/a'}\n"
            f"Impact: {'; '.join(summary.impact) or 'n/a'}\n"
            f"Risk: {summary.risk} — {summary.risk_reason}\n"
            f"Tests: {'; '.join(summary.recommended_tests) or 'n/a'}"
        )
    return "\n\n".join(parts)


@runtime_checkable
class LLMProvider(Protocol):
    """What the worker requires of any model backend."""

    name: str

    async def summarize_change(self, request: ChangeAnalysisRequest) -> ChangeSummary: ...

    async def synthesize(
        self, request: ChangeAnalysisRequest, summaries: list[tuple[str, ChangeSummary]]
    ) -> ChangeSummary: ...

    async def aclose(self) -> None: ...


def parse_summary_json(text: str) -> ChangeSummary:
    """Validate model output against the ChangeSummary schema.

    Tolerates a fenced code block, which some models still emit even in
    JSON mode, but never tries to parse free-form Markdown.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    try:
        payload: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"model did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LLMResponseError("model returned JSON that is not an object")
    try:
        return ChangeSummary.model_validate(payload)
    except ValueError as exc:
        raise LLMResponseError(f"model output failed schema validation: {exc}") from exc


class BaseLLMProvider(abc.ABC):
    """Shared prompt handling, retries and backoff for HTTP-based providers."""

    name = "base"

    def __init__(self, *, max_retries: int = 2) -> None:
        self.max_retries = max_retries

    @abc.abstractmethod
    async def _complete(self, system_prompt: str, user_prompt: str) -> ChangeSummary:
        """Single provider call returning schema-conforming output."""

    async def aclose(self) -> None:  # pragma: no cover - overridden where needed
        return None

    async def _complete_with_retries(self, system_prompt: str, user_prompt: str) -> ChangeSummary:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._complete(system_prompt, user_prompt)
            except LLMError as exc:
                last_error = exc
                if attempt >= self.max_retries or not getattr(exc, "retryable", True):
                    break
                delay = min(2**attempt + random.uniform(0, 0.5), 20)
                log.warning(
                    "llm call failed, retrying",
                    extra={"provider": self.name, "attempt": attempt + 1, "error": str(exc)[:300]},
                )
                await asyncio.sleep(delay)
        raise last_error or LLMError("llm call failed")

    async def summarize_change(self, request: ChangeAnalysisRequest) -> ChangeSummary:
        prompt = build_user_prompt(request)
        # Prompts contain proprietary source; only ever logged at DEBUG.
        log.debug("llm prompt built", extra={"provider": self.name, "chars": len(prompt)})
        return await self._complete_with_retries(SYSTEM_PROMPT, prompt)

    async def synthesize(
        self, request: ChangeAnalysisRequest, summaries: list[tuple[str, ChangeSummary]]
    ) -> ChangeSummary:
        prompt = build_synthesis_prompt(request, summaries)
        return await self._complete_with_retries(SYNTHESIS_PROMPT, prompt)


class RetryableLLMError(LLMError):
    """Transient provider failure (429/5xx/timeout)."""

    retryable = True


class FatalLLMError(LLMError):
    """Permanent provider failure (bad key, bad request)."""

    retryable = False
