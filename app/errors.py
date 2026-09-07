"""Typed exceptions so the worker can distinguish retryable from fatal errors."""

from __future__ import annotations


class ChangeBotError(Exception):
    """Base class for all application errors."""


class ConfigError(ChangeBotError):
    """Invalid or missing configuration."""


class WebhookVerificationError(ChangeBotError):
    """Signature, token or replay verification failed."""


class GitError(ChangeBotError):
    """A git subprocess failed, timed out, or was given unsafe input."""


class UnknownRevisionError(GitError):
    """A SHA is not present in the mirror even after a fetch (force-push/GC)."""


class LLMError(ChangeBotError):
    """The LLM provider failed or returned unusable output."""


class LLMResponseError(LLMError):
    """The provider answered, but not with schema-conforming JSON."""


class SlackError(ChangeBotError):
    """Delivering the Slack notification failed."""


class PermanentJobError(ChangeBotError):
    """The job can never succeed; fail it without burning retries."""
