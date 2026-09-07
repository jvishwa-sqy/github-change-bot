"""Unified-diff parser.

Git output is the source of truth for what changed, so this module turns
`git diff` text into structured ChangedFile records without shelling out
again. Keeping it pure makes every edge case (add / delete / rename / binary /
mode-only) directly testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models import ChangedFile, ChangeStatus

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_HEADER_RE = re.compile(r"^diff --git ")


def _unquote_path(raw: str) -> str:
    """Undo git's C-style quoting (`"pa\\th"`), leaving plain paths untouched."""
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        return raw
    inner = raw[1:-1]
    try:
        # Escapes are octal byte escapes of the UTF-8 encoding.
        return (
            inner.encode("latin-1", "backslashreplace")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8")
        )
    except (UnicodeDecodeError, UnicodeEncodeError):
        return inner


def _strip_prefix(path: str) -> str | None:
    """Strip git's a/ or b/ prefix. Returns None for /dev/null."""
    path = _unquote_path(path.strip())
    if path == "/dev/null":
        return None
    if len(path) > 2 and path[1] == "/" and path[0] in "abcijwo":
        return path[2:]
    return path


@dataclass
class _Block:
    """Accumulator for one file's section of a unified diff."""

    lines: list[str] = field(default_factory=list)
    old_path: str | None = None
    new_path: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_copy: bool = False
    is_binary: bool = False
    additions: int = 0
    deletions: int = 0
    touched_lines: set[int] = field(default_factory=set)
    header_paths: tuple[str, str] | None = None


def _header_paths_from_diff_line(line: str) -> tuple[str, str] | None:
    """Best-effort split of `diff --git a/x b/y` (ambiguous with spaces)."""
    rest = line[len("diff --git ") :].strip()
    if rest.startswith('"'):
        return None  # quoted forms are handled via the ---/+++ lines
    midpoint = rest.find(" b/")
    if midpoint == -1:
        return None
    left, right = rest[:midpoint], rest[midpoint + 1 :]
    stripped_left, stripped_right = _strip_prefix(left), _strip_prefix(right)
    if stripped_left is None or stripped_right is None:
        return None
    return stripped_left, stripped_right


def _finalise(block: _Block) -> ChangedFile | None:
    """Turn an accumulated block into a ChangedFile."""
    if not block.lines:
        return None

    old_path = block.rename_from or block.old_path
    new_path = block.rename_to or block.new_path
    if old_path is None and new_path is None and block.header_paths:
        old_path, new_path = block.header_paths
    if block.is_new:
        old_path = None
        new_path = new_path or (block.header_paths[1] if block.header_paths else None)
    if block.is_deleted:
        new_path = None
        old_path = old_path or (block.header_paths[0] if block.header_paths else None)

    if block.is_new:
        status = ChangeStatus.ADDED
    elif block.is_deleted:
        status = ChangeStatus.DELETED
    elif block.is_copy:
        status = ChangeStatus.COPIED
    elif block.rename_from is not None or (old_path and new_path and old_path != new_path):
        status = ChangeStatus.RENAMED
    else:
        status = ChangeStatus.MODIFIED

    return ChangedFile(
        old_path=old_path,
        new_path=new_path,
        status=status,
        additions=block.additions,
        deletions=block.deletions,
        diff="\n".join(block.lines).rstrip("\n"),
        is_binary=block.is_binary,
        changed_ranges=_coalesce(block.touched_lines),
    )


def _coalesce(lines: set[int]) -> list[tuple[int, int]]:
    """Turn individual touched line numbers into contiguous ranges."""
    ordered = sorted(n for n in lines if n >= 1)
    if not ordered:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number <= previous + 1:
            previous = number
            continue
        ranges.append((start, previous))
        start = previous = number
    ranges.append((start, previous))
    return ranges


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """Parse `git diff` output into one ChangedFile per touched path."""
    if not text.strip():
        return []

    files: list[ChangedFile] = []
    block: _Block | None = None
    in_hunk = False
    new_line = 0

    for raw_line in text.splitlines():
        if _DIFF_HEADER_RE.match(raw_line):
            if block is not None and (finished := _finalise(block)):
                files.append(finished)
            block = _Block(lines=[raw_line])
            block.header_paths = _header_paths_from_diff_line(raw_line)
            in_hunk = False
            continue

        if block is None:
            continue  # preamble before the first file header
        block.lines.append(raw_line)

        if raw_line.startswith("@@"):
            in_hunk = True
            if match := _HUNK_RE.match(raw_line):
                # Track the new-file line number as the hunk is walked, so the
                # recorded ranges are the lines that actually changed rather
                # than the whole hunk (which includes context lines and would
                # spill into neighbouring functions).
                new_line = int(match.group(3))
            continue

        if in_hunk:
            if raw_line.startswith("+"):
                block.additions += 1
                block.touched_lines.add(new_line)
                new_line += 1
            elif raw_line.startswith("-"):
                block.deletions += 1
                # A removed line has no new-file position; anchor it where the
                # deletion happened so the surrounding code is still found.
                block.touched_lines.add(new_line)
            elif raw_line.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:
                new_line += 1  # context line
            continue

        # --- still inside the extended header of this file ---
        if raw_line.startswith("new file mode"):
            block.is_new = True
        elif raw_line.startswith("deleted file mode"):
            block.is_deleted = True
        elif raw_line.startswith("rename from "):
            block.rename_from = _strip_prefix(raw_line[len("rename from ") :]) or None
        elif raw_line.startswith("rename to "):
            block.rename_to = _strip_prefix(raw_line[len("rename to ") :]) or None
        elif raw_line.startswith("copy from "):
            block.is_copy = True
            block.rename_from = _strip_prefix(raw_line[len("copy from ") :]) or None
        elif raw_line.startswith("copy to "):
            block.is_copy = True
            block.rename_to = _strip_prefix(raw_line[len("copy to ") :]) or None
        elif raw_line.startswith("--- "):
            block.old_path = _strip_prefix(raw_line[4:])
        elif raw_line.startswith("+++ "):
            block.new_path = _strip_prefix(raw_line[4:])
        elif raw_line.startswith("Binary files ") or raw_line.startswith("GIT binary patch"):
            block.is_binary = True

    if block is not None and (finished := _finalise(block)):
        files.append(finished)
    return files


def parse_numstat(text: str) -> dict[str, tuple[int, int]]:
    """Parse `git diff --numstat` into {path: (additions, deletions)}.

    Binary files report "-" counts and are recorded as (0, 0).
    """
    result: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[-1]
        result[_unquote_path(path)] = (
            int(added) if added.isdigit() else 0,
            int(deleted) if deleted.isdigit() else 0,
        )
    return result


def parse_name_status(text: str) -> list[tuple[str, str, str | None]]:
    """Parse `git diff --name-status -z` into (status, path, new_path) tuples."""
    entries: list[tuple[str, str, str | None]] = []
    fields = [f for f in text.split("\0") if f != ""]
    index = 0
    while index < len(fields):
        code = fields[index]
        index += 1
        if index >= len(fields):
            break
        path = fields[index]
        index += 1
        new_path: str | None = None
        if code and code[0] in {"R", "C"} and index < len(fields):
            new_path = fields[index]
            index += 1
        entries.append((code, path, new_path))
    return entries
