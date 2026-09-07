"""Persistent bare git mirrors and exact diff extraction.

Design rules:
  * one mirror per project, reused forever — never a fresh clone per push;
  * every git invocation is an argument list (never ``shell=True``);
  * every SHA is validated against a strict hex pattern before use;
  * credentials never persist in the mirror config and never reach the logs.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from app.diff_parser import parse_name_status, parse_numstat, parse_unified_diff
from app.errors import GitError, UnknownRevisionError
from app.models import ChangedFile, ChangeStatus, DiffResult

log = logging.getLogger(__name__)

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
ZERO_SHA = "0" * 40
# git returns 128 for "unknown revision"; 1 just means "differences found".
_DIFF_OK_CODES = frozenset({0, 1})


def is_valid_sha(value: str) -> bool:
    """True for a plausible git object id (never for the zero SHA)."""
    return bool(value) and value != ZERO_SHA and bool(SHA_RE.match(value))


def require_sha(value: str, *, label: str = "sha") -> str:
    """Validate a SHA before it is placed on a git command line."""
    if not is_valid_sha(value):
        raise GitError(f"unsafe or missing {label}: {value!r}")
    return value.lower()


def sanitize_url(url: str) -> str:
    """Strip any embedded credentials so a URL is safe to log."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if parts.netloc and "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    return url


def short(sha: str) -> str:
    return sha[:8] if sha and sha != ZERO_SHA else "-"


@dataclass(frozen=True)
class GitConfig:
    """Everything the git layer needs, injected rather than read globally."""

    repos_dir: Path
    ssh_command: str | None = None
    timeout_seconds: int = 600
    token: str | None = None


class GitMirror:
    """A bare mirror of one repository, plus the read operations we need."""

    def __init__(self, project_id: int, repo_url: str, config: GitConfig) -> None:
        self.project_id = project_id
        self.repo_url = repo_url
        self.config = config
        self.path = config.repos_dir / f"{project_id}.git"

    # ------------------------------------------------------------ subprocess
    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"  # never block waiting for credentials
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["LC_ALL"] = "C"
        if self.config.ssh_command:
            env["GIT_SSH_COMMAND"] = self.config.ssh_command
        else:
            env.setdefault(
                "GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
            )
        return env

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        ok_codes: frozenset[int] | None = None,
        binary: bool = False,  # noqa: ARG002 - documents that stdout stays raw
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a git command as an argument list. Never uses a shell."""
        command = ["git", "-c", "core.quotePath=false", "-c", "credential.helper=", *args]
        try:
            completed = subprocess.run(  # noqa: S603 - argument list, no shell
                command,
                cwd=str(cwd) if cwd else None,
                env=self._env(),
                capture_output=True,
                timeout=timeout or self.config.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError("git executable not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git timed out after {exc.timeout}s: {args[0]}") from exc

        allowed = ok_codes or frozenset({0})
        if check and completed.returncode not in allowed:
            stderr = completed.stderr.decode("utf-8", "replace").strip()
            # The URL may carry a token; never let it reach a log line.
            stderr = stderr.replace(self._auth_url(), sanitize_url(self.repo_url))
            raise GitError(f"git {args[0]} failed ({completed.returncode}): {stderr[:1000]}")
        return completed

    def _text(self, completed: subprocess.CompletedProcess[bytes]) -> str:
        return completed.stdout.decode("utf-8", "replace")

    def git(self, *args: str, **kwargs: object) -> str:
        """Run a git command inside this mirror and return stdout as text."""
        return self._text(self.run(["--git-dir", str(self.path), *args], **kwargs))  # type: ignore[arg-type]

    # ------------------------------------------------------------ remote URL
    def _auth_url(self) -> str:
        """Clone URL with a token injected for HTTPS remotes (never logged)."""
        if not self.config.token or not self.repo_url.startswith("https://"):
            return self.repo_url
        parts = urlsplit(self.repo_url)
        if "@" in parts.netloc:
            return self.repo_url
        netloc = f"x-access-token:{quote(self.config.token, safe='')}@{parts.netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    # -------------------------------------------------------------- lifecycle
    @property
    def exists(self) -> bool:
        return (self.path / "HEAD").exists() or (self.path / "objects").is_dir()

    def ensure(self) -> None:
        """Create the mirror on first use; no-op afterwards."""
        if self.exists:
            return
        self.config.repos_dir.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".tmp")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

        started = time.monotonic()
        log.info(
            "cloning mirror",
            extra={"project_id": self.project_id, "repo": sanitize_url(self.repo_url)},
        )
        self.run(["clone", "--mirror", "--quiet", self._auth_url(), str(staging)])
        # Replace any credentialed URL with the plain one so no token is stored.
        self.run(["--git-dir", str(staging), "remote", "set-url", "origin", self.repo_url])
        staging.rename(self.path)
        self.path.chmod(0o700)
        log.info(
            "mirror ready",
            extra={"project_id": self.project_id, "seconds": round(time.monotonic() - started, 2)},
        )

    def fetch(self) -> None:
        """Incrementally update the mirror. Cheap: only new objects transfer."""
        started = time.monotonic()
        self.run(
            [
                "--git-dir",
                str(self.path),
                "fetch",
                "--prune",
                "--force",
                "--quiet",
                "--no-tags",
                self._auth_url(),
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ]
        )
        log.debug(
            "mirror fetched",
            extra={"project_id": self.project_id, "seconds": round(time.monotonic() - started, 2)},
        )

    # ----------------------------------------------------------------- reads
    def has_object(self, sha: str) -> bool:
        """True if the commit is present locally."""
        if not is_valid_sha(sha):
            return False
        completed = self.run(
            ["--git-dir", str(self.path), "cat-file", "-e", f"{sha}^{{commit}}"],
            check=False,
        )
        return completed.returncode == 0

    def ensure_objects(self, *shas: str) -> None:
        """Fetch if any SHA is missing; raise if it is still missing after."""
        missing = [s for s in shas if is_valid_sha(s) and not self.has_object(s)]
        if not missing:
            return
        self.fetch()
        still_missing = [s for s in missing if not self.has_object(s)]
        if still_missing:
            raise UnknownRevisionError(
                "commits not present in the mirror after fetch "
                f"(force-push or GC?): {', '.join(short(s) for s in still_missing)}"
            )

    def rev_parse(self, revision: str) -> str | None:
        completed = self.run(
            [
                "--git-dir",
                str(self.path),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{revision}^{{commit}}",
            ],
            check=False,
        )
        if completed.returncode != 0:
            return None
        return self._text(completed).strip() or None

    def first_parent(self, sha: str) -> str | None:
        """Parent of a commit, or None for a root commit."""
        require_sha(sha, label="commit")
        return self.rev_parse(f"{sha}^")

    def merge_base(self, a: str, b: str) -> str | None:
        completed = self.run(
            ["--git-dir", str(self.path), "merge-base", require_sha(a), require_sha(b)],
            check=False,
        )
        if completed.returncode != 0:
            return None
        return self._text(completed).strip() or None

    def commit_subject(self, sha: str) -> str:
        return self.git("log", "-1", "--format=%s", require_sha(sha), check=False).strip()

    def file_content(self, sha: str, path: str) -> str | None:
        """Read a blob at a revision. Returns None when absent or binary."""
        require_sha(sha, label="commit")
        completed = self.run(
            ["--git-dir", str(self.path), "show", f"{sha}:{path}"],
            check=False,
            binary=True,
        )
        if completed.returncode != 0:
            return None
        raw = completed.stdout
        if b"\0" in raw[:8000]:  # NUL byte => treat as binary, never send to an LLM
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", "replace")

    def list_files(self, revision: str) -> list[str]:
        """All tracked file paths at a revision."""
        output = self.git("ls-tree", "-r", "--name-only", "-z", revision)
        return [p for p in output.split("\0") if p]

    def blob_size(self, sha: str, path: str) -> int:
        """Size in bytes of a blob, or -1 if it does not exist."""
        completed = self.run(
            ["--git-dir", str(self.path), "cat-file", "-s", f"{require_sha(sha)}:{path}"],
            check=False,
        )
        if completed.returncode != 0:
            return -1
        text = self._text(completed).strip()
        return int(text) if text.isdigit() else -1

    # ------------------------------------------------------------------ diff
    def diff_stat(self, before: str, after: str) -> str:
        return self.git(
            "diff",
            "--stat",
            "--find-renames",
            require_sha(before),
            require_sha(after),
            ok_codes=_DIFF_OK_CODES,
        ).strip()

    def diff_name_status(self, before: str, after: str) -> list[tuple[str, str, str | None]]:
        output = self.git(
            "diff",
            "--name-status",
            "--find-renames",
            "-z",
            require_sha(before),
            require_sha(after),
            ok_codes=_DIFF_OK_CODES,
        )
        return parse_name_status(output)

    def diff_numstat(self, before: str, after: str) -> dict[str, tuple[int, int]]:
        output = self.git(
            "diff",
            "--numstat",
            "--find-renames",
            require_sha(before),
            require_sha(after),
            ok_codes=_DIFF_OK_CODES,
        )
        return parse_numstat(output)

    def diff_files(
        self,
        before: str,
        after: str,
        *,
        unified: int = 3,
        paths: list[str] | None = None,
    ) -> list[ChangedFile]:
        """Structured per-file diff between two commits.

        `paths` limits the diff to a pathspec, which is how the caller keeps
        work proportional to the change rather than to repository size.
        """
        args = [
            "diff",
            "--find-renames",
            f"--unified={unified}",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            require_sha(before),
            require_sha(after),
        ]
        if paths:
            args += ["--", *paths]
        text = self.git(*args, ok_codes=_DIFF_OK_CODES)
        files = parse_unified_diff(text)

        # Binary files report no +/- lines; fill counts in from numstat.
        if any(f.is_binary for f in files):
            numstat = self.diff_numstat(before, after)
            for changed in files:
                if changed.is_binary and (counts := numstat.get(changed.path)):
                    changed.additions, changed.deletions = counts
        return files


def resolve_baseline(
    mirror: GitMirror,
    *,
    before_sha: str,
    after_sha: str,
    default_branch: str | None = None,
) -> str | None:
    """Pick the commit to diff `after_sha` against.

    Normal push: the reported `before` SHA, when the mirror still has it.
    Branch creation (zero `before`): the merge-base with the default branch,
    else the new commit's first parent. None means "no safe baseline" — the
    caller then sends a branch-created notice instead of summarising history.
    """
    if is_valid_sha(before_sha) and mirror.has_object(before_sha):
        return before_sha

    if default_branch:
        for candidate in (
            default_branch,
            f"origin/{default_branch}",
            f"refs/heads/{default_branch}",
        ):
            resolved = mirror.rev_parse(candidate)
            if resolved and resolved != after_sha:
                base = mirror.merge_base(resolved, after_sha)
                if base and base != after_sha:
                    return base
                break

    parent = mirror.first_parent(after_sha)
    if parent:
        return parent
    return None


def build_diff_result(
    mirror: GitMirror,
    *,
    baseline_sha: str,
    after_sha: str,
    ignore_matcher,  # Callable[[str], bool]
    max_changed_files: int,
    max_file_diff_chars: int,
    max_total_diff_chars: int,
    unified: int = 3,
) -> DiffResult:
    """Compute the bounded, ignore-filtered diff for a push.

    All limits are applied here so nothing downstream can accidentally hand an
    unbounded diff to the LLM.
    """
    baseline = require_sha(baseline_sha, label="baseline")
    head = require_sha(after_sha, label="after")

    name_status = mirror.diff_name_status(baseline, head)
    total_files = len(name_status)

    # Decide what to analyse *before* asking git for patch text, so ignored
    # trees (node_modules, dist, lockfiles) never cost us any diff generation.
    keep_paths: list[str] = []
    ignored: list[str] = []
    for code, path, new_path in name_status:
        effective = new_path or path
        if ignore_matcher(effective) and (new_path is None or ignore_matcher(path)):
            ignored.append(effective)
            continue
        keep_paths.append(effective)
        if new_path is not None and code.startswith(("R", "C")):
            keep_paths.append(path)  # include the old path so rename detection holds

    dropped: list[str] = []
    if not keep_paths:
        return DiffResult(
            files=[],
            stat_text=mirror.diff_stat(baseline, head),
            total_files=total_files,
            ignored_files=ignored,
            baseline_sha=baseline,
        )

    files = mirror.diff_files(baseline, head, unified=unified, paths=keep_paths)

    # Largest changes first: if the cap bites, keep the most interesting files.
    files.sort(key=lambda f: f.additions + f.deletions, reverse=True)
    if len(files) > max_changed_files:
        dropped = [f.path for f in files[max_changed_files:]]
        files = files[:max_changed_files]

    total_diff_chars = 0
    kept: list[ChangedFile] = []
    for changed in files:
        if changed.is_binary:
            changed.diff = f"(binary file, {changed.status.value})"
        elif len(changed.diff) > max_file_diff_chars:
            changed.diff = changed.diff[:max_file_diff_chars] + "\n… [diff truncated]"
            changed.truncated = True
        if total_diff_chars + len(changed.diff) > max_total_diff_chars:
            dropped.append(changed.path)
            continue
        total_diff_chars += len(changed.diff)
        kept.append(changed)

    kept.sort(key=lambda f: f.path)
    numstat = mirror.diff_numstat(baseline, head)
    total_additions = sum(a for a, _ in numstat.values())
    total_deletions = sum(d for _, d in numstat.values())

    return DiffResult(
        files=kept,
        stat_text=mirror.diff_stat(baseline, head),
        total_additions=total_additions,
        total_deletions=total_deletions,
        total_files=total_files,
        ignored_files=ignored,
        dropped_files=dropped,
        baseline_sha=baseline,
    )


__all__ = [
    "ChangeStatus",
    "GitConfig",
    "GitMirror",
    "build_diff_result",
    "is_valid_sha",
    "require_sha",
    "resolve_baseline",
    "sanitize_url",
    "short",
]
