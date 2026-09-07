"""Shared fixtures.

Environment defaults are set before any app module is imported so that
Settings validation (which requires a model key and a webhook secret) does not
depend on the developer's shell.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("LLM_PROVIDER", "null")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key")
os.environ.setdefault("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Never read a developer's real .env during the test run.
os.environ["BOT_ENV_FILE"] = "/nonexistent/.env"

from app.git_repo import GitConfig, GitMirror  # noqa: E402
from app.settings import Settings  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "LC_ALL": "C",
}


class GitSandbox:
    """A throwaway git repository used to produce real diffs in tests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-b", "main", "-q")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=GIT_ENV,
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def write_bytes(self, relative: str, content: bytes) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def remove(self, relative: str) -> None:
        self.git("rm", "-q", "--", relative)

    def move(self, source: str, destination: str) -> None:
        (self.path / destination).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", source, destination)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, "--allow-empty")
        return self.head

    @property
    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    @property
    def url(self) -> str:
        return str(self.path)


@pytest.fixture
def sandbox(tmp_path: Path) -> GitSandbox:
    return GitSandbox(tmp_path / "origin")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a temporary data directory."""
    instance = Settings(
        github_webhook_secret="test-webhook-secret",
        slack_webhook_url="https://hooks.slack.com/services/T0/B0/test",
        llm_provider="null",
        bot_data_dir=tmp_path / "data",
        log_level="WARNING",
    )
    instance.ensure_directories()
    return instance


@pytest.fixture
def git_config(settings: Settings) -> GitConfig:
    return GitConfig(repos_dir=settings.repos_dir, timeout_seconds=60)


@pytest.fixture
def mirror_factory(git_config: GitConfig):
    """Builds a GitMirror for a sandbox repository."""

    def build(sandbox: GitSandbox, project_id: int = 1) -> GitMirror:
        return GitMirror(project_id, sandbox.url, git_config)

    return build


@pytest.fixture
def mirror(sandbox: GitSandbox, mirror_factory) -> Iterator[GitMirror]:
    instance = mirror_factory(sandbox)
    yield instance
