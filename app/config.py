"""Application configuration, loaded from environment variables / .env."""

from __future__ import annotations

import logging
import os
import runpy
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

log = logging.getLogger(__name__)


class PythonConfigSource(PydanticBaseSettingsSource):
    """Read non-secret settings from the root-level ``config.py`` file."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, object]:
        path = os.getenv("BOT_CONFIG_FILE", "config.py")
        if not os.path.isfile(path):
            return {}
        values = runpy.run_path(path).get("SETTINGS", {})
        if not isinstance(values, dict):
            raise ValueError(f"{path} must define SETTINGS as a dictionary")
        return values


def _readable_env_file() -> str | None:
    """Path of the .env to load, or None if there is nothing readable there.

    The service account can legitimately be unable to read a .env that a
    developer left in the working directory (deployed secrets live in
    /etc/git-change-bot.env instead). Returning None skips the dotenv source,
    so an unreadable file is ignored rather than crashing startup with a
    PermissionError from deep inside the settings machinery.
    """
    path = os.getenv("BOT_ENV_FILE", ".env")
    if os.access(path, os.R_OK):
        return path
    if os.path.exists(path):
        log.warning("ignoring unreadable env file", extra={"path": path})
    return None


# Files that are never worth an LLM token: generated code, lockfiles, vendored
# trees and binaries. Users can replace the whole list via IGNORE_PATTERNS.
DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    "node_modules/**",
    "vendor/**",
    "dist/**",
    "build/**",
    "coverage/**",
    ".venv/**",
    "venv/**",
    "generated/**",
    "**/__pycache__/**",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.ico",
    "*.pdf",
    "*.zip",
    "*.gz",
    "*.tar",
    "*.bin",
    "*.so",
    "*.dll",
    "*.dylib",
    "*.class",
    "*.jar",
    "*.pyc",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.mp4",
    "*.mp3",
    "*.parquet",
)


class Settings(BaseSettings):
    """Runtime configuration. Validated once at process start."""

    model_config = SettingsConfigDict(
        env_file=_readable_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            PythonConfigSource(settings_cls),
            file_secret_settings,
        )

    # ---------------------------------------------------------------- GitHub
    # HMAC-SHA256 shared secret configured on the GitHub webhook ("Secret").
    github_webhook_secret: SecretStr | None = None
    # Optional plain-token header, for proxies/forwarders that cannot sign.
    github_legacy_secret_token: SecretStr | None = None
    # Optional PAT used only for HTTPS clone URLs (SSH deploy keys preferred).
    github_token: SecretStr | None = None
    # Reject a delivery whose replayed signature is older than this.
    webhook_max_age_seconds: int = 300
    # Refuse unsigned requests entirely (recommended in production).
    require_webhook_signature: bool = True

    # ----------------------------------------------------------------- Slack
    slack_webhook_url: SecretStr | None = None
    slack_timeout_seconds: float = 10.0
    # Send a cheap deterministic Slack card when a push has nothing analysable.
    notify_on_ignored_only: bool = False
    notify_on_branch_delete: bool = True
    notify_on_branch_create: bool = True

    # ------------------------------------------------------------------- LLM
    llm_provider: Literal["google", "null"] = "google"
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2

    google_api_key: SecretStr | None = None
    google_model: str = "gemini-2.5-flash"
    google_api_base: str = "https://generativelanguage.googleapis.com/v1beta"
    # 0 disables Gemini 2.5 "thinking" tokens, which dominate cost on a
    # short, well-scoped task like change summarisation. Raise for harder
    # analysis; set to null to use the model default.
    google_thinking_budget: int | None = 0

    # ------------------------------------------------------------- Storage
    bot_data_dir: Path = Path("/var/lib/git-change-bot")

    # ---------------------------------------------------------------- Git
    git_ssh_command: str | None = None
    git_timeout_seconds: int = 600
    # Mirrors older than this are refreshed before a diff; otherwise reused.
    git_clone_depth_note: str = ""  # mirrors are always full; kept for clarity

    # ------------------------------------------------------- Cost / limits
    max_diff_chars: int = 60_000
    max_context_chars: int = 90_000
    max_file_context_chars: int = 16_000
    max_changed_files: int = 80
    max_file_diff_chars: int = 12_000
    context_window_lines: int = 50
    # Above this many analysable files, switch to hierarchical (grouped) mode.
    hierarchical_file_threshold: int = 25
    hierarchical_max_groups: int = 8

    # ----------------------------------------------------------- Queue/worker
    max_job_attempts: int = 5
    worker_poll_seconds: float = 2.0
    # A claimed job whose worker died is re-queued after this many seconds.
    job_lease_seconds: int = 1800
    retry_backoff_seconds: int = 30

    # -------------------------------------------------------------- Logging
    log_level: str = "INFO"
    log_json: bool = False

    # -------------------------------------------------------------- Filtering
    ignore_patterns: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_IGNORE_PATTERNS)
    )
    # Extra patterns appended to the defaults (does not replace them).
    extra_ignore_patterns: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Only analyse pushes to these refs; empty means "all branches".
    watched_branches: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator(
        "ignore_patterns",
        "extra_ignore_patterns",
        "watched_branches",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept comma-separated env values as well as JSON arrays."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                # `NoDecode` keeps blank CSV values from being treated as
                # invalid JSON by pydantic-settings, so parse explicit JSON
                # lists here instead.
                import json

                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_provider_credentials(self) -> Settings:
        if self.llm_provider == "google" and not self.google_api_key:
            raise ValueError("LLM_PROVIDER=google requires GOOGLE_API_KEY to be set")
        if self.require_webhook_signature and not (
            self.github_webhook_secret or self.github_legacy_secret_token
        ):
            raise ValueError(
                "GITHUB_WEBHOOK_SECRET (or GITHUB_LEGACY_SECRET_TOKEN) must be set, "
                "or REQUIRE_WEBHOOK_SIGNATURE=false for local development"
            )
        return self

    # ------------------------------------------------------------- Derived
    @property
    def all_ignore_patterns(self) -> list[str]:
        return [*self.ignore_patterns, *self.extra_ignore_patterns]

    @property
    def db_path(self) -> Path:
        return self.bot_data_dir / "queue.sqlite3"

    @property
    def repos_dir(self) -> Path:
        return self.bot_data_dir / "repos"

    @property
    def indexes_dir(self) -> Path:
        return self.bot_data_dir / "indexes"

    @property
    def locks_dir(self) -> Path:
        return self.bot_data_dir / "locks"

    def ensure_directories(self) -> None:
        """Create the data directories this process needs. Safe to re-run."""
        for path in (self.bot_data_dir, self.repos_dir, self.indexes_dir, self.locks_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached; clear the cache in tests)."""
    return Settings()


def load_settings_lenient() -> Settings:
    """Settings for offline tools (bootstrap, inspection).

    Those tools touch git and the index but never the LLM or the webhook, so a
    missing model key or webhook secret must not stop them from running.
    """
    try:
        return Settings()
    except ValueError:
        return Settings(llm_provider="null", require_webhook_signature=False)
