"""Domain models shared across the webhook receiver, worker and LLM layer."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

ZERO_SHA = "0" * 40


class ChangeStatus(StrEnum):
    """Normalised git change status for a single file."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PushEventKind(StrEnum):
    """What the before/after SHA pair actually represents."""

    NORMAL = "normal"
    BRANCH_CREATED = "branch_created"
    BRANCH_DELETED = "branch_deleted"


class PushEvent(BaseModel):
    """The subset of a GitHub push webhook the bot actually needs."""

    model_config = ConfigDict(frozen=True)

    project_id: int
    project_name: str  # e.g. "acme/ai-caller-core"
    repo_url: str  # clone URL used for the mirror
    web_url: str | None = None
    ref: str  # e.g. "refs/heads/feature/x"
    before_sha: str
    after_sha: str
    author_name: str = "unknown"
    author_username: str = ""
    commit_count: int = 0
    commit_messages: list[str] = Field(default_factory=list)
    compare_url: str | None = None
    default_branch: str | None = None

    @property
    def branch(self) -> str:
        for prefix in ("refs/heads/", "refs/tags/"):
            if self.ref.startswith(prefix):
                return self.ref[len(prefix) :]
        return self.ref

    @property
    def is_branch_ref(self) -> bool:
        return self.ref.startswith("refs/heads/")

    @property
    def kind(self) -> PushEventKind:
        if self.after_sha == ZERO_SHA:
            return PushEventKind.BRANCH_DELETED
        if self.before_sha == ZERO_SHA:
            return PushEventKind.BRANCH_CREATED
        return PushEventKind.NORMAL

    @property
    def dedupe_key(self) -> str:
        """Identity of a push; replays of the same delivery collapse onto it."""
        return f"{self.project_id}:{self.ref}:{self.before_sha}:{self.after_sha}"

    @classmethod
    def from_github_payload(cls, payload: dict[str, Any], *, prefer_ssh: bool = True) -> Self:
        """Build a PushEvent from a GitHub `push` webhook body.

        Raises KeyError/TypeError style errors as ValueError so the webhook
        endpoint can answer 400 instead of 500 on a malformed body.
        """
        try:
            repo = payload["repository"] or {}
            ref = payload["ref"]
            before = payload["before"]
            after = payload["after"]
        except (KeyError, TypeError) as exc:  # malformed / not a push payload
            raise ValueError(f"missing required push field: {exc}") from exc

        commits = payload.get("commits") or []
        head = payload.get("head_commit") or {}
        pusher = payload.get("pusher") or {}
        sender = payload.get("sender") or {}
        head_author = head.get("author") or {}

        ssh_url = repo.get("ssh_url") or repo.get("git_ssh_url") or ""
        https_url = repo.get("clone_url") or repo.get("git_http_url") or repo.get("url") or ""
        repo_url = (ssh_url if prefer_ssh else https_url) or https_url or ssh_url
        if not repo_url:
            raise ValueError("push payload has no usable clone URL")

        project_id = repo.get("id")
        if project_id is None:
            raise ValueError("push payload has no repository id")

        return cls(
            project_id=int(project_id),
            project_name=repo.get("full_name") or repo.get("name") or str(project_id),
            repo_url=repo_url,
            web_url=repo.get("html_url") or repo.get("url"),
            ref=ref,
            before_sha=str(before).lower(),
            after_sha=str(after).lower(),
            author_name=head_author.get("name") or pusher.get("name") or "unknown",
            author_username=(
                head_author.get("username") or sender.get("login") or pusher.get("name") or ""
            ),
            commit_count=len(commits),
            commit_messages=[c.get("message", "").strip() for c in commits if c.get("message")][
                :20
            ],
            compare_url=payload.get("compare"),
            default_branch=repo.get("default_branch"),
        )


class ChangedFile(BaseModel):
    """One file touched by a push, with its unified diff."""

    old_path: str | None = None
    new_path: str | None = None
    status: ChangeStatus
    additions: int = 0
    deletions: int = 0
    diff: str = ""
    is_binary: bool = False
    truncated: bool = False
    # 1-based inclusive line ranges in the *new* file touched by this diff.
    changed_ranges: list[tuple[int, int]] = Field(default_factory=list)

    @property
    def path(self) -> str:
        """Best display path: the new path, falling back to the old one."""
        return self.new_path or self.old_path or "<unknown>"

    @property
    def exists_after(self) -> bool:
        return self.status is not ChangeStatus.DELETED and self.new_path is not None


class DiffResult(BaseModel):
    """Everything the diff stage learned about a push."""

    files: list[ChangedFile] = Field(default_factory=list)
    stat_text: str = ""
    total_additions: int = 0
    total_deletions: int = 0
    total_files: int = 0  # before ignore-filtering / capping
    ignored_files: list[str] = Field(default_factory=list)
    dropped_files: list[str] = Field(default_factory=list)  # cut by max_changed_files
    baseline_sha: str = ""  # SHA actually diffed against (may differ from before_sha)

    @property
    def has_analysable_changes(self) -> bool:
        return bool(self.files)


class SymbolKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    STRUCT = "struct"
    INTERFACE = "interface"
    TYPE = "type"
    CONSTANT = "constant"


class Symbol(BaseModel):
    """A top-level or nested named definition found by the deterministic parser."""

    name: str  # qualified, e.g. "DotcomListener.register_tools"
    kind: SymbolKind
    start_line: int
    end_line: int


class FileIndex(BaseModel):
    """Repo-map entry for one source file."""

    path: str
    language: str
    symbols: list[Symbol] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    loc: int = 0

    @property
    def symbol_names(self) -> list[str]:
        return [s.name for s in self.symbols]


class RepoMap(BaseModel):
    """Persistent, deterministically built map of a repository."""

    project_id: int
    project_name: str = ""
    commit_sha: str = ""
    updated_at: datetime | None = None
    files: dict[str, FileIndex] = Field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.files.values())


class FileContext(BaseModel):
    """Bounded, redacted context assembled for one changed file."""

    path: str
    language: str = "unknown"
    status: ChangeStatus
    additions: int = 0
    deletions: int = 0
    diff: str = ""
    # Enclosing functions/classes (Python: exact via AST; others: line window).
    code_context: str = ""
    context_kind: Literal["symbol", "window", "none"] = "none"
    symbols: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)

    def char_size(self) -> int:
        return len(self.diff) + len(self.code_context)


class ChangeAnalysisRequest(BaseModel):
    """The exact, bounded payload handed to an LLM provider."""

    project_name: str
    branch: str
    author: str
    before_sha: str
    after_sha: str
    commit_messages: list[str] = Field(default_factory=list)
    stat_text: str = ""
    total_additions: int = 0
    total_deletions: int = 0
    files: list[FileContext] = Field(default_factory=list)
    repo_overview: str = ""
    notes: list[str] = Field(default_factory=list)
    # Set for one leg of a hierarchical (grouped) analysis.
    group_name: str | None = None

    def char_size(self) -> int:
        return sum(f.char_size() for f in self.files) + len(self.repo_overview)


class ChangeSummary(BaseModel):
    """Structured LLM output. This schema is what the model must produce."""

    summary: str
    changes: list[str] = Field(default_factory=list)
    affected_components: list[str] = Field(default_factory=list)
    impact: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    risk_reason: str = ""
    recommended_tests: list[str] = Field(default_factory=list)


# Schema handed to providers that take an explicit JSON schema (Gemini's
# OpenAPI subset rejects $defs/additionalProperties, so it is written out
# rather than derived from ChangeSummary.model_json_schema()).
# tests/test_llm.py asserts it stays in sync with the model.
CHANGE_SUMMARY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences describing the change in product/behavioural terms.",
        },
        "changes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete behavioural changes, one per entry.",
        },
        "affected_components": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Named subsystems, modules or services affected.",
        },
        "impact": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Runtime or product impact of the change.",
        },
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "risk_reason": {"type": "string"},
        "recommended_tests": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific tests a reviewer should run.",
        },
    },
    "required": [
        "summary",
        "changes",
        "affected_components",
        "impact",
        "risk",
        "risk_reason",
        "recommended_tests",
    ],
}


class Job(BaseModel):
    """A queued push event, as stored in SQLite."""

    id: int
    dedupe_key: str
    project_id: int
    project_name: str
    repo_url: str
    ref: str
    before_sha: str
    after_sha: str
    author_name: str
    author_username: str
    commit_count: int
    payload_json: str
    status: JobStatus
    attempt_count: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    available_at: datetime | None = None
    last_error: str | None = None

    def push_event(self) -> PushEvent:
        """Rehydrate the original event from the stored payload."""
        return PushEvent.model_validate_json(self.payload_json)
