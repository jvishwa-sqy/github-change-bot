"""Context extraction: exact functions for Python, bounded windows otherwise."""

from __future__ import annotations

from app.context import (
    ContextBuilder,
    ContextLimits,
    group_files_by_subsystem,
    python_enclosing_spans,
    render_spans,
)
from app.models import ChangedFile, ChangeStatus, DiffResult, PushEvent, RepoMap
from app.repo_map import index_source

SOURCE = '''"""Listener."""
import os

from app.language_tools import register_language_tools


class DotcomListener:
    """Handles inbound calls."""

    def __init__(self, config):
        self.config = config

    def register_tools(self):
        tools = []
        for name in self.config.tools:
            tools.append(name)
        register_language_tools(tools)
        return tools

    def teardown(self):
        return None


def unrelated_helper():
    return 42
'''

LINES = SOURCE.splitlines()
REGISTER_START = LINES.index("    def register_tools(self):") + 1
REGISTER_BODY = LINES.index("        register_language_tools(tools)") + 1


def event() -> PushEvent:
    return PushEvent(
        project_id=1,
        project_name="acme/app",
        repo_url="git@github.com:acme/app.git",
        ref="refs/heads/feature/x",
        before_sha="a" * 40,
        after_sha="b" * 40,
        author_name="Vishwa",
    )


def changed_file(path: str, ranges: list[tuple[int, int]], diff: str = "") -> ChangedFile:
    return ChangedFile(
        old_path=path,
        new_path=path,
        status=ChangeStatus.MODIFIED,
        additions=1,
        deletions=1,
        diff=diff or "@@ -1 +1 @@\n-old\n+new",
        changed_ranges=ranges,
    )


# ------------------------------------------------------------- span finding
def test_change_inside_method_selects_the_whole_method() -> None:
    (span,) = python_enclosing_spans(SOURCE, [(REGISTER_BODY, REGISTER_BODY)])
    assert span.label == "function DotcomListener.register_tools"
    assert span.start == REGISTER_START
    assert span.end >= REGISTER_BODY


def test_change_in_class_body_selects_the_class() -> None:
    docstring_line = LINES.index('    """Handles inbound calls."""') + 1
    (span,) = python_enclosing_spans(SOURCE, [(docstring_line, docstring_line)])
    assert span.label == "class DotcomListener"


def test_module_level_change_has_no_enclosing_symbol() -> None:
    assert python_enclosing_spans(SOURCE, [(2, 2)]) == []


def test_overlapping_spans_are_merged() -> None:
    spans = python_enclosing_spans(
        SOURCE, [(REGISTER_BODY, REGISTER_BODY), (REGISTER_BODY + 1, REGISTER_BODY + 1)]
    )
    assert len(spans) == 1


def test_unparseable_source_yields_no_spans() -> None:
    assert python_enclosing_spans("def broken(:\n", [(1, 1)]) == []


def test_render_spans_is_bounded() -> None:
    from app.context import Span

    text, truncated = render_spans(SOURCE, [Span(1, len(LINES), "all")], max_chars=120)
    assert truncated
    assert len(text) <= 200


# ------------------------------------------------------------ file contexts
def test_python_context_contains_the_full_changed_function() -> None:
    builder = ContextBuilder(read_file=lambda _p: SOURCE)
    context = builder.build_file_context(
        changed_file("app/listener.py", [(REGISTER_BODY, REGISTER_BODY)]), budget=16_000
    )
    assert context.context_kind == "symbol"
    assert "def register_tools(self):" in context.code_context
    assert "register_language_tools(tools)" in context.code_context
    # Unrelated code is not shipped to the model.
    assert "def unrelated_helper" not in context.code_context
    assert "def teardown" not in context.code_context


def test_context_includes_imports_for_a_file_not_yet_in_the_map() -> None:
    builder = ContextBuilder(read_file=lambda _p: SOURCE)
    context = builder.build_file_context(
        changed_file("app/listener.py", [(REGISTER_BODY, REGISTER_BODY)]), budget=16_000
    )
    assert "app.language_tools" in context.imports


def test_repo_map_supplies_symbols_and_reverse_imports() -> None:
    repo_map = RepoMap(project_id=1)
    repo_map.files["app/listener.py"] = index_source("app/listener.py", SOURCE)
    repo_map.files["app/caller.py"] = index_source(
        "app/caller.py", "from app.listener import DotcomListener\n"
    )
    builder = ContextBuilder(read_file=lambda _p: SOURCE, repo_map=repo_map)
    context = builder.build_file_context(
        changed_file("app/listener.py", [(REGISTER_BODY, REGISTER_BODY)]), budget=16_000
    )
    assert "DotcomListener.register_tools" in context.symbols
    assert context.related_files == ["app/caller.py"]


def test_unsupported_language_uses_a_bounded_window() -> None:
    source = "\n".join(f"line {i}" for i in range(1, 501))
    builder = ContextBuilder(read_file=lambda _p: source, limits=ContextLimits(window_lines=10))
    context = builder.build_file_context(changed_file("src/app.rb", [(100, 101)]), budget=16_000)
    assert context.context_kind == "window"
    assert "line 90" in context.code_context
    assert "line 111" in context.code_context
    assert "line 50" not in context.code_context


def test_added_file_gets_no_duplicate_context() -> None:
    builder = ContextBuilder(read_file=lambda _p: SOURCE)
    added = ChangedFile(
        new_path="app/new.py",
        status=ChangeStatus.ADDED,
        additions=5,
        diff="@@ -0,0 +1,2 @@\n+def a():\n+    pass",
        changed_ranges=[(1, 2)],
    )
    assert builder.build_file_context(added, budget=16_000).code_context == ""


def test_deleted_file_reads_no_source() -> None:
    def read(_path: str) -> str:
        raise AssertionError("deleted files must not be read at the new revision")

    builder = ContextBuilder(read_file=read)
    deleted = ChangedFile(
        old_path="app/gone.py",
        status=ChangeStatus.DELETED,
        deletions=3,
        diff="@@ -1,3 +0,0 @@\n-a\n-b\n-c",
        changed_ranges=[(1, 1)],
    )
    assert builder.build_file_context(deleted, budget=16_000).code_context == ""


# ------------------------------------------------------------ full requests
def test_build_request_is_redacted_and_bounded() -> None:
    leaky = 'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"'
    diff = f"@@ -1 +1 @@\n-old\n+{leaky}"
    builder = ContextBuilder(read_file=lambda _p: SOURCE)
    result = DiffResult(
        files=[changed_file("app/listener.py", [(REGISTER_BODY, REGISTER_BODY)], diff=diff)],
        total_additions=1,
        total_deletions=1,
        baseline_sha="a" * 40,
        ignored_files=["package-lock.json"],
    )
    request = builder.build(event(), result)

    assert "wJalrXUtnFEMIK" not in request.files[0].diff
    assert "[REDACTED]" in request.files[0].diff
    assert request.branch == "feature/x"
    assert any("skipped as generated" in note for note in request.notes)


def test_total_context_budget_is_respected() -> None:
    big = "\n".join(f"def fn_{i}():\n    return {i}" for i in range(400))
    builder = ContextBuilder(
        read_file=lambda _p: big,
        limits=ContextLimits(max_context_chars=3000, max_file_context_chars=1500),
    )
    files = [changed_file(f"app/mod_{i}.py", [(10, 12)]) for i in range(10)]
    request = builder.build(event(), DiffResult(files=files, baseline_sha="a" * 40))
    assert request.char_size() <= 3000 + 2000  # diffs are small; context is capped


# ----------------------------------------------------------------- grouping
def test_grouping_by_subsystem() -> None:
    files = [
        changed_file("app/routing/a.py", [(1, 1)]),
        changed_file("app/routing/b.py", [(1, 1)]),
        changed_file("app/mcp/c.py", [(1, 1)]),
        changed_file("tests/test_a.py", [(1, 1)]),
        changed_file("README.md", [(1, 1)]),
    ]
    groups = dict(group_files_by_subsystem(files, max_groups=8))
    assert set(groups) == {"app/routing", "app/mcp", "tests", "(root)"}
    assert len(groups["app/routing"]) == 2


def test_grouping_collapses_when_too_many_groups() -> None:
    files = [changed_file(f"svc_{i}/pkg/file.py", [(1, 1)]) for i in range(20)]
    groups = group_files_by_subsystem(files, max_groups=4)
    assert len(groups) <= 4
    assert sum(len(group) for _name, group in groups) == 20


def test_grouping_empty() -> None:
    assert group_files_by_subsystem([]) == []
