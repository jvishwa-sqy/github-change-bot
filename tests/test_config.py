"""Configuration loading and validation."""

from __future__ import annotations

import logging
import os

import pytest

from app.config import Settings, _readable_env_file, load_settings_lenient
from app.logging_setup import configure_logging


def test_unreadable_env_file_is_ignored(tmp_path, monkeypatch) -> None:
    """A .env the service account cannot read must not crash startup."""
    secret_env = tmp_path / ".env"
    secret_env.write_text("GOOGLE_API_KEY=should-not-be-read\n")
    secret_env.chmod(0o000)
    monkeypatch.setenv("BOT_ENV_FILE", str(secret_env))
    try:
        if os.access(secret_env, os.R_OK):  # running as root: the check cannot apply
            pytest.skip("cannot make a file unreadable as this user")
        assert _readable_env_file() is None
    finally:
        secret_env.chmod(0o600)


def test_readable_env_file_is_used(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_API_KEY=from-file\nGITHUB_WEBHOOK_SECRET=s\n")
    monkeypatch.setenv("BOT_ENV_FILE", str(env_file))
    assert _readable_env_file() == str(env_file)


def test_missing_env_file_is_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_ENV_FILE", str(tmp_path / "absent.env"))
    assert _readable_env_file() is None


def test_google_provider_requires_a_key() -> None:
    with pytest.raises(ValueError, match="GOOGLE_API_KEY"):
        Settings(github_webhook_secret="s", llm_provider="google", google_api_key=None)


def test_webhook_secret_is_required_by_default() -> None:
    with pytest.raises(ValueError, match="GITHUB_WEBHOOK_SECRET"):
        Settings(llm_provider="null", github_webhook_secret=None)


def test_unsigned_mode_must_be_explicit() -> None:
    settings = Settings(
        llm_provider="null", github_webhook_secret=None, require_webhook_signature=False
    )
    assert settings.require_webhook_signature is False


def test_lenient_loader_works_without_credentials(monkeypatch, tmp_path) -> None:
    """Offline tools must run without a model key or webhook secret."""
    monkeypatch.setenv("BOT_ENV_FILE", str(tmp_path / "absent.env"))
    for name in ("GOOGLE_API_KEY", "GITHUB_WEBHOOK_SECRET", "LLM_PROVIDER", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings_lenient()
    assert settings.llm_provider == "null"


def test_csv_list_settings_are_split() -> None:
    settings = Settings(
        github_webhook_secret="s",
        llm_provider="null",
        extra_ignore_patterns="*.generated.ts, proto/**",
        watched_branches="main,develop",
    )
    assert settings.extra_ignore_patterns == ["*.generated.ts", "proto/**"]
    assert settings.watched_branches == ["main", "develop"]


def test_blank_list_settings_in_env_file_are_allowed(tmp_path) -> None:
    """The documented `EXTRA_IGNORE_PATTERNS=` form means an empty list."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=null\n"
        "REQUIRE_WEBHOOK_SIGNATURE=false\n"
        "EXTRA_IGNORE_PATTERNS=\n"
        "WATCHED_BRANCHES=\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.extra_ignore_patterns == []
    assert settings.watched_branches == []


def test_derived_paths(tmp_path) -> None:
    settings = Settings(
        github_webhook_secret="s", llm_provider="null", bot_data_dir=tmp_path / "data"
    )
    assert settings.db_path == tmp_path / "data" / "queue.sqlite3"
    assert settings.repos_dir == tmp_path / "data" / "repos"
    settings.ensure_directories()
    assert settings.locks_dir.is_dir()


def test_secrets_are_not_exposed_by_repr() -> None:
    settings = Settings(
        github_webhook_secret="super-secret-value",
        llm_provider="google",
        google_api_key="AIza-super-secret",
    )
    assert "super-secret-value" not in repr(settings)
    assert "AIza-super-secret" not in repr(settings)


def test_http_client_urls_are_not_logged_at_info() -> None:
    configure_logging("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING
