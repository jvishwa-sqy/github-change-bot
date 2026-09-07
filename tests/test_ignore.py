"""Ignore-pattern matching."""

from __future__ import annotations

import pytest

from app.ignore import IgnoreMatcher
from app.settings import DEFAULT_IGNORE_PATTERNS

matcher = IgnoreMatcher(DEFAULT_IGNORE_PATTERNS)


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/left-pad/index.js",
        "frontend/node_modules/react/index.js",
        "vendor/github.com/pkg/errors/errors.go",
        "dist/main.js",
        "build/output.css",
        "coverage/lcov.info",
        "package-lock.json",
        "frontend/package-lock.json",
        "yarn.lock",
        "poetry.lock",
        "Cargo.lock",
        "app/static/bundle.min.js",
        "app/static/bundle.js.map",
        "assets/logo.png",
        "docs/manual.pdf",
        "generated/protobuf/service_pb2.py",
        "app/__pycache__/module.cpython-312.pyc",
    ],
)
def test_low_value_paths_are_ignored(path: str) -> None:
    assert matcher.matches(path)


@pytest.mark.parametrize(
    "path",
    [
        "app/main.py",
        "src/components/Widget.tsx",
        "cmd/server/main.go",
        "distributed/worker.py",  # not "dist/"
        "app/node_modules_helper.py",
        "docs/architecture.md",
        "Dockerfile",
    ],
)
def test_source_paths_are_kept(path: str) -> None:
    assert not matcher.matches(path)


def test_custom_patterns_replace_defaults() -> None:
    custom = IgnoreMatcher(["*.generated.ts", "proto/**"])
    assert custom.matches("api/client.generated.ts")
    assert custom.matches("proto/service.proto")
    assert not custom.matches("package-lock.json")


def test_anchored_pattern_matches_only_at_the_root() -> None:
    anchored = IgnoreMatcher(["/build/**"])
    assert anchored.matches("build/out.js")
    assert not anchored.matches("app/build/out.js")


def test_directory_pattern() -> None:
    assert IgnoreMatcher(["tmp/"]).matches("tmp/scratch.py")


def test_empty_pattern_list_ignores_nothing() -> None:
    empty = IgnoreMatcher([])
    assert not empty.matches("anything.png")


def test_filter_helper() -> None:
    kept = matcher.filter(["app/main.py", "yarn.lock", "src/a.ts"])
    assert kept == ["app/main.py", "src/a.ts"]
