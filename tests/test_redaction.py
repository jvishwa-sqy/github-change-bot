"""Secret redaction before anything leaves the process."""

from __future__ import annotations

import pytest

from app.redaction import redact, redact_findings


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('OPENAI_API_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz123"', "sk-proj-abcdef"),
        ("aws_key = AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("token = ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghij"),
        ("gitlab = glpat-abcdefghijklmnopqrst", "glpat-abcdefghij"),
        ("GOOGLE_API_KEY=AIzaSyA12345678901234567890123456789012345", "AIzaSyA1234567890"),
        ("slack = xoxb-1234567890-abcdefghij", "xoxb-1234567890"),
        (
            "url = https://hooks.slack.com/services/T000/B000/abcdefg",
            "hooks.slack.com/services/T000",
        ),
        ("password: sup3rs3cretvalue", "sup3rs3cretvalue"),
        ("DATABASE_URL = postgres://user:hunter2pass@db.internal:5432/app", "hunter2pass"),
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijkl",
            "eyJzdWIiOiIxMjMifQ",
        ),
        ("client_secret= 'abcdef123456ghijkl'", "abcdef123456ghijkl"),
    ],
)
def test_secret_is_removed(text: str, secret: str) -> None:
    result = redact(text)
    assert secret not in result
    assert "[REDACTED]" in result


def test_private_key_block_is_removed() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAx7Nq\nabc123\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact(text)
    assert "MIIEowIBAAKCAQEAx7Nq" not in result
    assert "PRIVATE-KEY" in result


@pytest.mark.parametrize(
    "text",
    [
        'api_key = os.environ["API_KEY"]',
        "password = None",
        'secret: ""',
        "token = process.env.TOKEN",
        "def get_token(self) -> str:",
        'logger.info("token refreshed")',
        "self.api_key = api_key",
    ],
)
def test_non_secrets_are_left_alone(text: str) -> None:
    assert redact(text) == text


def test_redaction_is_idempotent() -> None:
    once = redact('API_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz"')
    assert redact(once) == once


def test_empty_input() -> None:
    assert redact("") == ""


def test_findings_report_counts_without_exposing_values() -> None:
    findings = redact_findings("key = AKIAIOSFODNN7EXAMPLE\nother = AKIAIOSFODNN7EXAMPLB")
    assert findings["aws_access_key_id"] == 2


def test_diff_body_is_redacted_line_by_line() -> None:
    diff = (
        "@@ -1,2 +1,3 @@\n"
        " def connect():\n"
        '-    return connect("postgres://u:oldpassword@db/app")\n'
        '+    return connect("postgres://u:newpassword@db/app")\n'
    )
    result = redact(diff)
    assert "oldpassword" not in result
    assert "newpassword" not in result
    assert "def connect():" in result  # surrounding code survives


@pytest.mark.parametrize(
    ("text", "expected_redacted"),
    [
        ("self.api_key = api_key", False),  # passthrough of a variable
        ("token = get_token(request)", False),  # a call, not a literal
        ("api_key: config_api_key", True),  # config-style value
        ("SECRET_KEY = mysecretvalue123", True),  # a literal that is not the key
        ('api_key = "literal-value-here"', True),  # always redact quoted values
    ],
)
def test_assignment_heuristics(text: str, expected_redacted: bool) -> None:
    assert ("[REDACTED]" in redact(text)) is expected_redacted
