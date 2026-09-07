"""The per-push analysis pipeline.

    git diff = truth · repo map = context · LLM = explanation · Slack = presentation

Blocking git work runs in a worker thread while holding the per-project lock;
the LLM and Slack calls are async and run without it, so a slow model never
blocks another project's git operations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from app.context import ContextBuilder, ContextLimits, group_files_by_subsystem
from app.git_repo import (
    GitConfig,
    GitMirror,
    build_diff_result,
    resolve_baseline,
    short,
)
from app.ignore import IgnoreMatcher
from app.llm.base import LLMProvider
from app.locks import project_lock
from app.models import (
    ChangeAnalysisRequest,
    ChangedFile,
    ChangeStatus,
    ChangeSummary,
    DiffResult,
    PushEvent,
    PushEventKind,
)
from app.repo_map import RepoMapStore
from app.settings import Settings
from app.slack import SlackNotifier, compare_link

log = logging.getLogger(__name__)

OutcomeStatus = Literal[
    "analyzed", "branch_created", "branch_deleted", "ignored_only", "no_changes", "skipped"
]


@dataclass
class AnalysisOutcome:
    """What happened to one job — used for logging and by the tests."""

    status: OutcomeStatus
    llm_calls: int = 0
    changed_files: int = 0
    diff_chars: int = 0
    context_chars: int = 0
    llm_ms: int = 0
    notified: bool = False
    summary: ChangeSummary | None = None


@dataclass
class PreparedAnalysis:
    """Result of the synchronous git phase."""

    diff: DiffResult
    request: ChangeAnalysisRequest | None = None
    groups: list[tuple[str, ChangeAnalysisRequest]] = field(default_factory=list)


class ChangeAnalyzer:
    """Runs one push event end to end."""

    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMProvider,
        slack: SlackNotifier,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.slack = slack
        self.ignore = IgnoreMatcher(settings.all_ignore_patterns)
        self.git_config = GitConfig(
            repos_dir=settings.repos_dir,
            ssh_command=settings.git_ssh_command,
            timeout_seconds=settings.git_timeout_seconds,
            token=(settings.github_token.get_secret_value() if settings.github_token else None),
        )

    # ------------------------------------------------------------------ entry
    async def analyze(self, event: PushEvent) -> AnalysisOutcome:
        """Analyse a push and notify Slack. Raises on unrecoverable errors."""
        if event.kind is PushEventKind.BRANCH_DELETED:
            return await self._branch_deleted(event)

        if self.settings.watched_branches and event.branch not in self.settings.watched_branches:
            log.info(
                "branch not watched; skipping",
                extra={"project_id": event.project_id, "branch": event.branch},
            )
            return AnalysisOutcome(status="skipped")

        started = time.monotonic()
        prepared = await asyncio.to_thread(self._prepare, event)
        git_ms = int((time.monotonic() - started) * 1000)

        diff = prepared.diff
        if prepared.request is None and not prepared.groups:
            return await self._nothing_to_analyze(event, diff)

        log.info(
            "diff computed",
            extra={
                "project_id": event.project_id,
                "project": event.project_name,
                "branch": event.branch,
                "before": short(diff.baseline_sha or event.before_sha),
                "after": short(event.after_sha),
                "changed_files": len(diff.files),
                "ignored_files": len(diff.ignored_files),
                "diff_chars": sum(len(f.diff) for f in diff.files),
                "git_ms": git_ms,
                "mode": "hierarchical" if prepared.groups else "single",
            },
        )

        llm_started = time.monotonic()
        if prepared.groups:
            summary, calls = await self._analyze_hierarchical(prepared)
        else:
            assert prepared.request is not None
            summary = await self.llm.summarize_change(prepared.request)
            calls = 1
        llm_ms = int((time.monotonic() - llm_started) * 1000)

        notified = False
        if self.slack.enabled:
            await self.slack.send_change(event, summary, diff)
            notified = True

        request = prepared.request or (prepared.groups[0][1] if prepared.groups else None)
        return AnalysisOutcome(
            status="analyzed",
            llm_calls=calls,
            changed_files=len(diff.files),
            diff_chars=sum(len(f.diff) for f in diff.files),
            context_chars=request.char_size() if request else 0,
            llm_ms=llm_ms,
            notified=notified,
            summary=summary,
        )

    # ------------------------------------------------------------ git phase
    def _prepare(self, event: PushEvent) -> PreparedAnalysis:
        """Blocking: mirror, fetch, diff, repo-map update, context assembly."""
        mirror = GitMirror(event.project_id, event.repo_url, self.git_config)

        with project_lock(self.settings.locks_dir, event.project_id):
            mirror.ensure()
            mirror.ensure_objects(event.after_sha)

            baseline = resolve_baseline(
                mirror,
                before_sha=event.before_sha,
                after_sha=event.after_sha,
                default_branch=event.default_branch,
            )
            if baseline is None:
                # New branch whose history has no safe baseline: refuse to
                # summarise an entire repository's worth of commits.
                return PreparedAnalysis(diff=DiffResult(baseline_sha=""))

            diff = build_diff_result(
                mirror,
                baseline_sha=baseline,
                after_sha=event.after_sha,
                ignore_matcher=self.ignore,
                max_changed_files=self.settings.max_changed_files,
                max_file_diff_chars=self.settings.max_file_diff_chars,
                max_total_diff_chars=self.settings.max_diff_chars,
            )
            if not diff.has_analysable_changes:
                return PreparedAnalysis(diff=diff)

            repo_map = self._update_repo_map(mirror, event, diff)

            builder = ContextBuilder(
                read_file=lambda path: mirror.file_content(event.after_sha, path),
                repo_map=repo_map,
                limits=ContextLimits(
                    max_context_chars=self.settings.max_context_chars,
                    max_file_context_chars=self.settings.max_file_context_chars,
                    window_lines=self.settings.context_window_lines,
                ),
            )

            if len(diff.files) <= self.settings.hierarchical_file_threshold:
                return PreparedAnalysis(diff=diff, request=builder.build(event, diff))

            # Large push: one request per subsystem, synthesised afterwards.
            groups = group_files_by_subsystem(
                diff.files, max_groups=self.settings.hierarchical_max_groups
            )
            per_group_limit = max(4000, self.settings.max_context_chars // max(len(groups), 1))
            grouped_builder = ContextBuilder(
                read_file=lambda path: mirror.file_content(event.after_sha, path),
                repo_map=repo_map,
                limits=ContextLimits(
                    max_context_chars=per_group_limit,
                    max_file_context_chars=min(
                        self.settings.max_file_context_chars, per_group_limit
                    ),
                    window_lines=self.settings.context_window_lines,
                ),
            )
            return PreparedAnalysis(
                diff=diff,
                groups=[
                    (name, grouped_builder.build(event, diff, group_name=name, files=files))
                    for name, files in groups
                ],
            )

    def _update_repo_map(self, mirror: GitMirror, event: PushEvent, diff: DiffResult):
        """Re-index only the files this push touched."""
        store = RepoMapStore(self.settings.indexes_dir, event.project_id)
        repo_map = store.load()
        repo_map.project_name = event.project_name

        modified = [f.new_path for f in diff.files if f.exists_after and f.new_path]
        deleted = [
            f.old_path for f in diff.files if f.status is ChangeStatus.DELETED and f.old_path
        ]
        renamed = [
            (f.old_path, f.new_path)
            for f in diff.files
            if f.status in (ChangeStatus.RENAMED, ChangeStatus.COPIED) and f.old_path and f.new_path
        ]

        updated = store.apply_changes(
            repo_map,
            read_file=lambda path: mirror.file_content(event.after_sha, path),
            modified=modified,
            deleted=deleted,
            renamed=renamed,
            commit_sha=event.after_sha,
        )
        try:
            store.save(updated)
        except OSError as exc:  # a broken index must not fail the analysis
            log.warning(
                "could not persist repo map",
                extra={"project_id": event.project_id, "error": str(exc)[:200]},
            )
        return updated

    # ------------------------------------------------------------ llm phase
    async def _analyze_hierarchical(self, prepared: PreparedAnalysis) -> tuple[ChangeSummary, int]:
        """Summarise each subsystem, then synthesise one overall answer."""
        results: list[tuple[str, ChangeSummary]] = []
        for name, request in prepared.groups:
            results.append((name, await self.llm.summarize_change(request)))

        base = prepared.groups[0][1]
        overall = ChangeAnalysisRequest(
            project_name=base.project_name,
            branch=base.branch,
            author=base.author,
            before_sha=base.before_sha,
            after_sha=base.after_sha,
            stat_text=prepared.diff.stat_text,
            total_additions=prepared.diff.total_additions,
            total_deletions=prepared.diff.total_deletions,
        )
        return await self.llm.synthesize(overall, results), len(results) + 1

    # -------------------------------------------------------- cheap paths
    async def _branch_deleted(self, event: PushEvent) -> AnalysisOutcome:
        """No diff exists and nothing can be analysed — never call the LLM."""
        notified = False
        if self.settings.notify_on_branch_delete and self.slack.enabled:
            await self.slack.send_simple(
                event,
                "Branch deleted",
                f"`{event.branch}` was deleted (was at `{short(event.before_sha)}`).",
            )
            notified = True
        return AnalysisOutcome(status="branch_deleted", notified=notified)

    async def _nothing_to_analyze(self, event: PushEvent, diff: DiffResult) -> AnalysisOutcome:
        """Branch creation without a baseline, or a push of ignored files only."""
        if not diff.baseline_sha:
            notified = False
            if self.settings.notify_on_branch_create and self.slack.enabled:
                await self.slack.send_simple(
                    event,
                    "Branch created",
                    f"`{event.branch}` was created at `{short(event.after_sha)}` "
                    f"({event.commit_count} commit(s)). No baseline was available, "
                    "so its full history was not summarised.",
                    link=compare_link(event, diff),
                )
                notified = True
            return AnalysisOutcome(status="branch_created", notified=notified)

        ignored_only = bool(diff.ignored_files)
        status: OutcomeStatus = "ignored_only" if ignored_only else "no_changes"
        log.info(
            "no analysable changes; skipping LLM",
            extra={
                "project_id": event.project_id,
                "branch": event.branch,
                "ignored_files": len(diff.ignored_files),
            },
        )
        notified = False
        if ignored_only and self.settings.notify_on_ignored_only and self.slack.enabled:
            await self.slack.send_simple(
                event,
                "Push skipped",
                f"{len(diff.ignored_files)} generated/vendored file(s) changed on "
                f"`{event.branch}`; nothing worth analysing.",
                link=compare_link(event, diff),
            )
            notified = True
        return AnalysisOutcome(status=status, notified=notified)


def summarise_changed_files(files: list[ChangedFile]) -> str:
    """Short one-line description of a change set, for log lines."""
    return ", ".join(f"{f.status.value[:3]}:{f.path}" for f in files[:5]) or "none"
