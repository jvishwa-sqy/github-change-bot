"""Unified-diff parsing: every status git can report."""

from __future__ import annotations

from app.diff_parser import parse_name_status, parse_numstat, parse_unified_diff
from app.models import ChangeStatus

MODIFIED = """diff --git a/app/service.py b/app/service.py
index 1111111..2222222 100644
--- a/app/service.py
+++ b/app/service.py
@@ -10,7 +10,8 @@ class Service:
     def run(self):
-        return 1
+        self.setup()
+        return 2
"""

ADDED = """diff --git a/app/new_module.py b/app/new_module.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/app/new_module.py
@@ -0,0 +1,3 @@
+def hello():
+    return "hi"
+
"""

DELETED = """diff --git a/legacy/old.py b/legacy/old.py
deleted file mode 100644
index 4444444..0000000
--- a/legacy/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    pass
"""

RENAMED = """diff --git a/app/old_name.py b/app/new_name.py
similarity index 92%
rename from app/old_name.py
rename to app/new_name.py
index 5555555..6666666 100644
--- a/app/old_name.py
+++ b/app/new_name.py
@@ -1,3 +1,3 @@
 def keep():
-    return 1
+    return 2
"""

BINARY = """diff --git a/assets/logo.png b/assets/logo.png
index 7777777..8888888 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""

PURE_RENAME = """diff --git a/a.py b/b.py
similarity index 100%
rename from a.py
rename to b.py
"""


def test_modified_file() -> None:
    (changed,) = parse_unified_diff(MODIFIED)
    assert changed.status is ChangeStatus.MODIFIED
    assert changed.old_path == changed.new_path == "app/service.py"
    assert (changed.additions, changed.deletions) == (2, 1)
    assert changed.changed_ranges == [(11, 12)]
    assert "self.setup()" in changed.diff


def test_added_file() -> None:
    (changed,) = parse_unified_diff(ADDED)
    assert changed.status is ChangeStatus.ADDED
    assert changed.old_path is None
    assert changed.new_path == "app/new_module.py"
    assert changed.additions == 3
    assert changed.exists_after


def test_deleted_file() -> None:
    (changed,) = parse_unified_diff(DELETED)
    assert changed.status is ChangeStatus.DELETED
    assert changed.new_path is None
    assert changed.old_path == "legacy/old.py"
    assert changed.deletions == 2
    assert not changed.exists_after


def test_renamed_file_with_edits() -> None:
    (changed,) = parse_unified_diff(RENAMED)
    assert changed.status is ChangeStatus.RENAMED
    assert changed.old_path == "app/old_name.py"
    assert changed.new_path == "app/new_name.py"
    assert (changed.additions, changed.deletions) == (1, 1)


def test_pure_rename_without_hunks() -> None:
    (changed,) = parse_unified_diff(PURE_RENAME)
    assert changed.status is ChangeStatus.RENAMED
    assert (changed.old_path, changed.new_path) == ("a.py", "b.py")
    assert changed.changed_ranges == []


def test_binary_file_is_flagged() -> None:
    (changed,) = parse_unified_diff(BINARY)
    assert changed.is_binary
    assert changed.path == "assets/logo.png"


def test_multiple_files_in_one_diff() -> None:
    files = parse_unified_diff(MODIFIED + ADDED + DELETED + RENAMED + BINARY)
    assert [f.path for f in files] == [
        "app/service.py",
        "app/new_module.py",
        "legacy/old.py",
        "app/new_name.py",
        "assets/logo.png",
    ]
    assert [f.status for f in files] == [
        ChangeStatus.MODIFIED,
        ChangeStatus.ADDED,
        ChangeStatus.DELETED,
        ChangeStatus.RENAMED,
        ChangeStatus.MODIFIED,
    ]


def test_empty_diff() -> None:
    assert parse_unified_diff("") == []
    assert parse_unified_diff("   \n") == []


def test_multiple_hunks_record_each_range() -> None:
    diff = MODIFIED + "@@ -80,3 +81,4 @@\n context\n+added\n"
    (changed,) = parse_unified_diff(diff)
    assert changed.changed_ranges == [(11, 12), (82, 82)]


def test_pure_deletion_hunk_anchors_a_range() -> None:
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -5,3 +4,0 @@\n-a\n-b\n-c\n"
    (changed,) = parse_unified_diff(diff)
    assert changed.changed_ranges == [(4, 4)]
    assert changed.deletions == 3


def test_quoted_path_is_unquoted() -> None:
    diff = (
        'diff --git "a/app/we ird\\"name.py" "b/app/we ird\\"name.py"\n'
        '--- "a/app/we ird\\"name.py"\n'
        '+++ "b/app/we ird\\"name.py"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    (changed,) = parse_unified_diff(diff)
    assert changed.path == 'app/we ird"name.py'


def test_parse_numstat() -> None:
    stats = parse_numstat("3\t1\tapp/a.py\n0\t5\tapp/b.py\n-\t-\tlogo.png\n")
    assert stats == {"app/a.py": (3, 1), "app/b.py": (0, 5), "logo.png": (0, 0)}


def test_parse_name_status_handles_renames() -> None:
    raw = "M\0app/a.py\0R096\0app/old.py\0app/new.py\0D\0gone.py\0"
    assert parse_name_status(raw) == [
        ("M", "app/a.py", None),
        ("R096", "app/old.py", "app/new.py"),
        ("D", "gone.py", None),
    ]


def test_added_file_ranges_cover_the_new_content() -> None:
    (changed,) = parse_unified_diff(ADDED)
    assert changed.changed_ranges == [(1, 3)]


def test_ranges_exclude_context_lines() -> None:
    """Context lines must not widen a range into a neighbouring function."""
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,7 +1,7 @@\n a\n b\n c\n-old\n+new\n e\n f\n g\n"
    )
    (changed,) = parse_unified_diff(diff)
    assert changed.changed_ranges == [(4, 4)]
