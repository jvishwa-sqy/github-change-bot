"""Secret redaction.

Everything that leaves this process for an LLM or Slack passes through
``redact``. Patterns are ordered from most specific to most general; each match
is replaced with a marker that preserves enough shape for the model to
understand the code without leaking the value.
"""

from __future__ import annotations

import re
from collections.abc import Callable

REDACTED = "[REDACTED]"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _redact_assignment(match: re.Match[str]) -> str:
    """Redact an assignment's value unless it is plainly a variable reference.

    ``self.api_key = api_key`` and ``token = get_token(...)`` pass values
    around rather than embedding them; blanking those makes real code harder
    to review without hiding anything. Anything quoted, and anything in a
    config-style ``key: value`` line, is still redacted.
    """
    prefix, key, separator, quote, value = match.group(1, 2, 3, 4, 5)
    if not quote and separator == "=":
        if "(" in value:
            return match.group(0)  # a call, not a literal
        if _IDENTIFIER_RE.match(value):
            # A bare name echoing the key is a passthrough, not a secret.
            tail = value.rsplit(".", 1)[-1].lower()
            if tail and tail in key.lower():
                return match.group(0)
    return prefix + REDACTED


# (name, compiled pattern, replacement). Group 1, where present, is preserved
# so assignments keep their left-hand side and stay readable to the model.
_PATTERNS: list[tuple[str, re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        f"{REDACTED}-PRIVATE-KEY",
    ),
    (
        "aws_access_key_id",
        re.compile(r"\b((?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16})\b"),
        REDACTED,
    ),
    (
        "github_token",
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,255})\b"),
        REDACTED,
    ),
    (
        "gitlab_token",
        re.compile(r"\b(glpat-[A-Za-z0-9_\-]{16,64})\b"),
        REDACTED,
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
        REDACTED,
    ),
    (
        "slack_webhook",
        re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/+_-]+"),
        REDACTED,
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
        REDACTED,
    ),
    (
        "jwt",
        re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        REDACTED,
    ),
    (
        "bearer",
        re.compile(r"(?i)\b(bearer|token|authorization:)\s+[A-Za-z0-9._\-+/=]{12,}"),
        r"\1 " + REDACTED,
    ),
    (
        "db_url_password",
        re.compile(
            r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s/@]{3,}(@)",
        ),
        r"\1" + REDACTED + r"\2",
    ),
    (
        # KEY = "value" / key: value / KEY=value, for secret-looking names.
        "assignment",
        re.compile(
            r"(?i)"
            r"([\"']?([A-Za-z0-9_.\-]*"
            r"(?:passwd|password|secret|api[_-]?key|apikey|access[_-]?key|"
            r"private[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
            r"client[_-]?secret|signing[_-]?key|webhook[_-]?url|credential|token)"
            r"[A-Za-z0-9_.\-]*)[\"']?\s*([:=])\s*)"
            r"(?![\"']?(?:''|\"\"|none|null|true|false|process\.env|os\.environ|\$\{|<|\{\{|\[REDACTED))"
            r"([\"']?)([^\s\"',;)\]}]{6,})\4",
            re.MULTILINE,
        ),
        _redact_assignment,
    ),
    (
        "generic_hex_secret",
        re.compile(r"(?i)\b(?:secret|token|key)[\"']?\s*[:=]\s*[\"']?([0-9a-f]{32,})\b"),
        REDACTED,
    ),
]


def redact(text: str) -> str:
    """Replace likely secrets in `text`. Safe to call on diffs and source."""
    if not text:
        return text
    for _name, pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_findings(text: str) -> dict[str, int]:
    """Count matches per rule. Used for logging *how many* secrets were hidden."""
    return {
        name: count for name, pattern, _ in _PATTERNS if (count := len(pattern.findall(text))) > 0
    }
