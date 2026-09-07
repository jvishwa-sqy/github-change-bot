"""Git mirror and diff extraction against real repositories."""

from __future__ import annotations

import pytest

from app.errors import GitError, UnknownRevisionError
from app.git_repo import (
    GitMirror,
    build_diff_result,
    is_valid_sha,
    require_sha,
    resolve_baseline,
    sanitize_url,
)
from app.ignore import IgnoreMatcher
from app.models import ChangeStatus

ZERO = "0" * 40


def build_diff(mirror: GitMirror, before: str, after: str, **overrides):
    params = {
        "ignore_matcher": IgnoreMatcher(["node_modules/**", "package-lock.json", "*.png"]),
        "max_changed_files": 80,
        "max_file_diff_chars": 12_000,
        "max_total_diff_chars": 60_000,
    }
    params.update(overrides)
    return build_diff_result(mirror, baseline_sha=before, after_sha=after, **params)


# ------------------------------------------------------------------ helpers
def test_sha_validation() -> None:
    assert is_valid_sha("a" * 40)
    assert is_valid_sha("abc1234")
    assert not is_valid_sha(ZERO)
    assert not is_valid_sha("")
    assert not is_valid_sha("main")
    assert not is_valid_sha("a" * 40 + "; rm -rf /")
    assert not is_valid_sha("--upload-pack=evil")


def test_require_sha_rejects_injection() -> None:
    with pytest.raises(GitError):
        require_sha("$(whoami)")


def test_sanitize_url_strips_credentials() -> None:
    assert (
        sanitize_url("https://x-access-token:ghp_secret@github.com/a/b.git")
        == "https://github.com/a/b.git"
    )
    assert sanitize_url("git@github.com:a/b.git") == "git@github.com:a/b.git"


# ------------------------------------------------------------------- mirror
def test_mirror_clone_and_incremental_fetch(sandbox, mirror) -> None:
    sandbox.write("app/service.py", "def run():\n    return 1\n")
    first = sandbox.commit("commit A")

    assert not mirror.exists
    mirror.ensure()
    assert mirror.exists
    assert (mirror.path / "HEAD").exists()
    assert not (mirror.path / "worktree").exists()  # bare mirror, no checkout
    assert mirror.has_object(first)

    # A second ensure() must not re-clone.
    mirror.ensure()

    sandbox.write("app/service.py", "def run():\n    return 2\n")
    second = sandbox.commit("commit B")
    assert not mirror.has_object(second)

    mirror.fetch()
    assert mirror.has_object(second)


def test_mirror_never_stores_credentials(sandbox, git_config) -> None:
    mirror = GitMirror(7, sandbox.url, git_config)
    sandbox.write("a.py", "x = 1\n")
    sandbox.commit("init")
    mirror.ensure()
    config = (mirror.path / "config").read_text()
    assert "x-access-token" not in config


def test_ensure_objects_fetches_missing_commit(sandbox, mirror) -> None:
    sandbox.write("a.py", "x = 1\n")
    sandbox.commit("A")
    mirror.ensure()

    sandbox.write("a.py", "x = 2\n")
    head = sandbox.commit("B")
    mirror.ensure_objects(head)  # triggers a fetch
    assert mirror.has_object(head)


def test_ensure_objects_raises_for_unknown_commit(sandbox, mirror) -> None:
    sandbox.write("a.py", "x = 1\n")
    sandbox.commit("A")
    mirror.ensure()
    with pytest.raises(UnknownRevisionError):
        mirror.ensure_objects("d" * 40)


# --------------------------------------------------------------------- diff
def test_diff_between_two_commits(sandbox, mirror) -> None:
    sandbox.write("app/service.py", "def run():\n    return 1\n")
    sandbox.write("README.md", "# project\n")
    first = sandbox.commit("commit A")

    sandbox.write("app/service.py", "def run():\n    setup()\n    return 2\n")
    sandbox.write("app/new_module.py", "def added():\n    return True\n")
    sandbox.remove("README.md")
    second = sandbox.commit("commit B")

    mirror.ensure()
    result = build_diff(mirror, first, second)

    by_path = {f.path: f for f in result.files}
    assert set(by_path) == {"app/service.py", "app/new_module.py", "README.md"}
    assert by_path["app/service.py"].status is ChangeStatus.MODIFIED
    assert by_path["app/new_module.py"].status is ChangeStatus.ADDED
    assert by_path["README.md"].status is ChangeStatus.DELETED
    assert "setup()" in by_path["app/service.py"].diff
    assert result.total_additions > 0
    assert "app/service.py" in result.stat_text
    assert result.baseline_sha == first


def test_diff_detects_renames(sandbox, mirror) -> None:
    body = "\n".join(f"def fn{i}():\n    return {i}\n" for i in range(20))
    sandbox.write("app/old_name.py", body)
    first = sandbox.commit("A")

    sandbox.move("app/old_name.py", "app/new_name.py")
    sandbox.write("app/new_name.py", body + "\ndef extra():\n    return 99\n")
    second = sandbox.commit("B")

    mirror.ensure()
    (changed,) = build_diff(mirror, first, second).files
    assert changed.status is ChangeStatus.RENAMED
    assert changed.old_path == "app/old_name.py"
    assert changed.new_path == "app/new_name.py"


def test_ignored_files_are_excluded(sandbox, mirror) -> None:
    sandbox.write("app/a.py", "x = 1\n")
    first = sandbox.commit("A")

    sandbox.write("package-lock.json", '{"lockfileVersion": 3}\n')
    sandbox.write("node_modules/left-pad/index.js", "module.exports = 1;\n")
    second = sandbox.commit("B")

    mirror.ensure()
    result = build_diff(mirror, first, second)
    assert result.files == []
    assert not result.has_analysable_changes
    assert sorted(result.ignored_files) == ["node_modules/left-pad/index.js", "package-lock.json"]


def test_binary_file_is_marked_and_not_dumped(sandbox, mirror) -> None:
    sandbox.write("app/a.py", "x = 1\n")
    first = sandbox.commit("A")
    sandbox.write_bytes("assets/blob.bin", bytes(range(256)) * 20)
    second = sandbox.commit("B")

    mirror.ensure()
    (changed,) = build_diff(mirror, first, second).files
    assert changed.is_binary
    assert changed.diff == "(binary file, added)"


def test_max_changed_files_cap(sandbox, mirror) -> None:
    sandbox.write("app/a.py", "x = 1\n")
    first = sandbox.commit("A")
    for index in range(10):
        sandbox.write(f"app/mod_{index}.py", f"value = {index}\n")
    second = sandbox.commit("B")

    mirror.ensure()
    result = build_diff(mirror, first, second, max_changed_files=4)
    assert len(result.files) == 4
    assert len(result.dropped_files) == 6


def test_per_file_diff_truncation(sandbox, mirror) -> None:
    sandbox.write("app/big.py", "x = 0\n")
    first = sandbox.commit("A")
    sandbox.write("app/big.py", "\n".join(f"value_{i} = {i}" for i in range(5000)))
    second = sandbox.commit("B")

    mirror.ensure()
    (changed,) = build_diff(mirror, first, second, max_file_diff_chars=2000).files
    assert changed.truncated
    assert len(changed.diff) <= 2000 + len("\n… [diff truncated]")


def test_file_content_and_listing(sandbox, mirror) -> None:
    sandbox.write("app/a.py", "x = 1\n")
    sandbox.write("docs/readme.md", "hello\n")
    head = sandbox.commit("A")
    mirror.ensure()

    assert mirror.file_content(head, "app/a.py") == "x = 1\n"
    assert mirror.file_content(head, "missing.py") is None
    assert sorted(mirror.list_files(head)) == ["app/a.py", "docs/readme.md"]
    assert mirror.blob_size(head, "app/a.py") == 6


def test_file_content_returns_none_for_binary(sandbox, mirror) -> None:
    sandbox.write_bytes("blob.bin", b"\x00\x01\x02binary")
    head = sandbox.commit("A")
    mirror.ensure()
    assert mirror.file_content(head, "blob.bin") is None


# ---------------------------------------------------------------- baselines
def test_baseline_is_the_before_sha_for_a_normal_push(sandbox, mirror) -> None:
    sandbox.write("a.py", "x = 1\n")
    first = sandbox.commit("A")
    sandbox.write("a.py", "x = 2\n")
    second = sandbox.commit("B")
    mirror.ensure()

    assert resolve_baseline(mirror, before_sha=first, after_sha=second) == first


def test_branch_creation_uses_merge_base_with_default_branch(sandbox, mirror) -> None:
    sandbox.write("a.py", "x = 1\n")
    base = sandbox.commit("A")
    sandbox.git("checkout", "-q", "-b", "feature/new")
    sandbox.write("a.py", "x = 2\n")
    sandbox.commit("B")
    sandbox.write("a.py", "x = 3\n")
    head = sandbox.commit("C")
    mirror.ensure()

    baseline = resolve_baseline(mirror, before_sha=ZERO, after_sha=head, default_branch="main")
    assert baseline == base


def test_branch_creation_falls_back_to_first_parent(sandbox, mirror) -> None:
    sandbox.write("a.py", "x = 1\n")
    first = sandbox.commit("A")
    sandbox.write("a.py", "x = 2\n")
    head = sandbox.commit("B")
    mirror.ensure()

    assert resolve_baseline(mirror, before_sha=ZERO, after_sha=head) == first


def test_root_commit_has_no_baseline(sandbox, mirror) -> None:
    sandbox.write("a.py", "x = 1\n")
    head = sandbox.commit("A")
    mirror.ensure()

    assert resolve_baseline(mirror, before_sha=ZERO, after_sha=head) is None


def test_missing_before_sha_falls_back_instead_of_failing(sandbox, mirror) -> None:
    """A force-push can reference a commit the mirror never received."""
    sandbox.write("a.py", "x = 1\n")
    first = sandbox.commit("A")
    sandbox.write("a.py", "x = 2\n")
    head = sandbox.commit("B")
    mirror.ensure()

    baseline = resolve_baseline(mirror, before_sha="c" * 40, after_sha=head)
    assert baseline == first
