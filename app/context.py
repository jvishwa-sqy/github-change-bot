"""Context assembly for the LLM.

The rule this module exists to enforce: send the diff plus *just enough*
surrounding code to explain behaviour — never the whole repository, never an
unbounded amount of any one file.

For Python the enclosing function/class is located exactly with the AST. For
other languages a bounded line window around each hunk is used. Everything is
capped, then redacted.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.models import (
    ChangeAnalysisRequest,
    ChangedFile,
    ChangeStatus,
    DiffResult,
    FileContext,
    PushEvent,
    RepoMap,
)
from app.redaction import redact
from app.repo_map import detect_language, get_extractor, summarise_repo_map

log = logging.getLogger(__name__)

# A file's context is skipped rather than truncated mid-symbol below this.
MIN_USEFUL_CONTEXT_CHARS = 200


@dataclass(frozen=True)
class ContextLimits:
    max_context_chars: int = 90_000
    max_file_context_chars: int = 16_000
    window_lines: int = 50


@dataclass(frozen=True)
class Span:
    """An inclusive 1-based line range with a human label."""

    start: int
    end: int
    label: str


def _merge_spans(spans: Sequence[Span]) -> list[Span]:
    """Merge overlapping spans so shared code is never sent twice."""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    merged = [ordered[0]]
    for span in ordered[1:]:
        last = merged[-1]
        if span.start <= last.end + 1:
            label = last.label if span.label in last.label else f"{last.label}, {span.label}"
            merged[-1] = Span(last.start, max(last.end, span.end), label)
        else:
            merged.append(span)
    return merged


def python_enclosing_spans(source: str, ranges: Sequence[tuple[int, int]]) -> list[Span]:
    """Find the smallest def/class in `source` that encloses each changed range.

    A change inside a method yields that method; a change to a class body
    outside any method yields the class. Returns [] if the file does not parse.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    @dataclass
    class _Node:
        start: int
        end: int
        label: str

    nodes: list[_Node] = []

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                start = min(
                    [child.lineno]
                    + [d.lineno for d in child.decorator_list if hasattr(d, "lineno")]
                )
                end = child.end_lineno or child.lineno
                name = f"{prefix}{child.name}"
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                nodes.append(_Node(start, end, f"{kind} {name}"))
                walk(child, prefix=f"{name}.")
            else:
                walk(child, prefix)

    walk(tree)

    spans: list[Span] = []
    for start, end in ranges:
        enclosing = [n for n in nodes if n.start <= end and n.end >= start]
        if not enclosing:
            continue
        # Smallest enclosing node = the actual function, not its module/class.
        best = min(enclosing, key=lambda n: n.end - n.start)
        spans.append(Span(best.start, best.end, best.label))
    return _merge_spans(spans)


def window_spans(total_lines: int, ranges: Sequence[tuple[int, int]], window: int) -> list[Span]:
    """Bounded line windows around each changed range."""
    spans = [
        Span(max(1, start - window), min(total_lines, end + window), "surrounding lines")
        for start, end in ranges
    ]
    return _merge_spans(spans)


def render_spans(source: str, spans: Iterable[Span], *, max_chars: int) -> tuple[str, bool]:
    """Render spans as numbered code blocks, stopping at `max_chars`."""
    lines = source.splitlines()
    chunks: list[str] = []
    used = 0
    truncated = False
    for span in spans:
        start = max(1, span.start)
        end = min(len(lines), span.end)
        if start > end:
            continue
        body = "\n".join(f"{number:>5}| {lines[number - 1]}" for number in range(start, end + 1))
        block = f"--- {span.label} (lines {start}-{end}) ---\n{body}"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > MIN_USEFUL_CONTEXT_CHARS:
                chunks.append(block[:remaining] + "\n… [context truncated]")
            truncated = True
            break
        chunks.append(block)
        used += len(block)
    return "\n\n".join(chunks), truncated


class ContextBuilder:
    """Assembles a bounded ChangeAnalysisRequest for a push."""

    def __init__(
        self,
        *,
        read_file: Callable[[str], str | None],
        repo_map: RepoMap | None = None,
        limits: ContextLimits | None = None,
    ) -> None:
        self._read_file = read_file
        self._repo_map = repo_map or RepoMap(project_id=0)
        self._limits = limits or ContextLimits()

    # ------------------------------------------------------------- per file
    def build_file_context(self, changed: ChangedFile, *, budget: int) -> FileContext:
        """Diff + surrounding code for one changed file, within `budget` chars."""
        path = changed.path
        language = detect_language(path)
        context = FileContext(
            path=path,
            language=language,
            status=changed.status,
            additions=changed.additions,
            deletions=changed.deletions,
            diff=changed.diff,
        )

        entry = self._repo_map.files.get(path)
        if entry is not None:
            context.symbols = entry.symbol_names[:25]
            context.imports = entry.imports[:25]

        code_budget = min(budget, self._limits.max_file_context_chars) - len(changed.diff)
        if (
            not changed.exists_after
            or changed.is_binary
            or changed.status is ChangeStatus.ADDED  # the diff already *is* the file
            or code_budget < MIN_USEFUL_CONTEXT_CHARS
            or not changed.changed_ranges
        ):
            return context

        source = self._read_file(path)
        if source is None:
            return context

        spans: list[Span] = []
        if language == "python":
            spans = python_enclosing_spans(source, changed.changed_ranges)
            context.context_kind = "symbol" if spans else "none"
        if not spans:
            spans = window_spans(
                source.count("\n") + 1, changed.changed_ranges, self._limits.window_lines
            )
            context.context_kind = "window" if spans else "none"

        rendered, _ = render_spans(source, spans, max_chars=code_budget)
        context.code_context = rendered
        if not rendered:
            context.context_kind = "none"

        # Imports of the file itself matter even when it is not yet in the map
        # (a file added by this very push, for example).
        if not context.imports and (extractor := get_extractor(language)) is not None:
            _symbols, imports = extractor.extract(source)
            context.imports = imports[:25]

        context.related_files = self._related_files(path)
        return context

    def _related_files(self, path: str, *, limit: int = 5) -> list[str]:
        """Files in the map that import this module. Cheap reverse lookup."""
        module = Path(path).with_suffix("").as_posix().replace("/", ".")
        stem = Path(path).stem
        if not self._repo_map.files or stem in {"__init__", "index", "mod"}:
            return []
        related: list[str] = []
        for other_path, entry in self._repo_map.files.items():
            if other_path == path:
                continue
            if any(
                imported == module or imported.endswith(f".{stem}") or imported.endswith(f"/{stem}")
                for imported in entry.imports
            ):
                related.append(other_path)
            if len(related) >= limit:
                break
        return related

    # ---------------------------------------------------------------- push
    def build(
        self,
        event: PushEvent,
        diff: DiffResult,
        *,
        group_name: str | None = None,
        files: Sequence[ChangedFile] | None = None,
    ) -> ChangeAnalysisRequest:
        """Assemble the full request, redacting everything that leaves here."""
        selected = list(files if files is not None else diff.files)
        budget = self._limits.max_context_chars
        contexts: list[FileContext] = []

        # Even split, so one huge file cannot starve the rest of the change.
        per_file = max(
            MIN_USEFUL_CONTEXT_CHARS,
            min(self._limits.max_file_context_chars, budget // max(len(selected), 1)),
        )
        for changed in selected:
            if budget <= 0:
                break
            context = self.build_file_context(changed, budget=min(per_file, budget))
            budget -= context.char_size()
            contexts.append(context)

        notes: list[str] = []
        if diff.ignored_files:
            notes.append(
                f"{len(diff.ignored_files)} file(s) were skipped as generated/vendored/binary."
            )
        if diff.dropped_files:
            notes.append(
                f"{len(diff.dropped_files)} file(s) were omitted to stay within size limits: "
                + ", ".join(diff.dropped_files[:10])
            )
        if any(f.truncated for f in selected):
            notes.append("Some diffs were truncated; judge only what is shown.")

        overview = summarise_repo_map(self._repo_map, [c.path for c in contexts])

        request = ChangeAnalysisRequest(
            project_name=event.project_name,
            branch=event.branch,
            author=event.author_name,
            before_sha=diff.baseline_sha or event.before_sha,
            after_sha=event.after_sha,
            commit_messages=event.commit_messages[:10],
            stat_text=diff.stat_text,
            total_additions=diff.total_additions,
            total_deletions=diff.total_deletions,
            files=contexts,
            repo_overview=overview,
            notes=notes,
            group_name=group_name,
        )
        return redact_request(request)


def redact_request(request: ChangeAnalysisRequest) -> ChangeAnalysisRequest:
    """Redact every free-text field that could carry a secret."""
    for context in request.files:
        context.diff = redact(context.diff)
        context.code_context = redact(context.code_context)
    request.commit_messages = [redact(message) for message in request.commit_messages]
    request.repo_overview = redact(request.repo_overview)
    return request


def group_files_by_subsystem(
    files: Sequence[ChangedFile], *, max_groups: int = 8
) -> list[tuple[str, list[ChangedFile]]]:
    """Group changed files by top directories, for hierarchical summarisation.

    Grouping is by the two leading path segments (``app/routing``,
    ``app/mcp``…), collapsing to the top segment when that yields too many
    groups, so a 150-file push becomes a handful of LLM calls rather than 150.
    """
    if not files:
        return []

    def key_for(path: str, depth: int) -> str:
        parts = Path(path).parts
        if len(parts) <= 1:
            return "(root)"
        return "/".join(parts[: min(depth, len(parts) - 1)])

    for depth in (2, 1):
        groups: dict[str, list[ChangedFile]] = {}
        for changed in files:
            groups.setdefault(key_for(changed.path, depth), []).append(changed)
        if len(groups) <= max_groups:
            return sorted(groups.items(), key=lambda item: item[0])

    # Still too many top-level directories: merge the smallest ones together.
    groups = {}
    for changed in files:
        groups.setdefault(key_for(changed.path, 1), []).append(changed)
    ordered = sorted(groups.items(), key=lambda item: len(item[1]), reverse=True)
    head = ordered[: max_groups - 1]
    tail: list[ChangedFile] = [f for _name, group in ordered[max_groups - 1 :] for f in group]
    if tail:
        head.append(("(other)", tail))
    return sorted(head, key=lambda item: item[0])
