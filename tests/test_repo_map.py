"""Deterministic repository-map extraction and incremental updates."""

from __future__ import annotations

from app.models import RepoMap, SymbolKind
from app.repo_map import (
    RepoMapStore,
    detect_language,
    index_source,
    is_source_file,
    summarise_repo_map,
)

PYTHON_SOURCE = '''"""Module docstring."""
import os
from app.config import get_settings
from . import sibling

TIMEOUT = 30


class Foo:
    """A class."""

    def bar(self):
        pass

    @staticmethod
    async def baz(x):
        return x


def top_level(a, b):
    def nested():
        return 1

    return nested()
'''


def test_python_symbols_are_qualified() -> None:
    entry = index_source("app/foo.py", PYTHON_SOURCE)
    assert entry.language == "python"
    assert "Foo" in entry.symbol_names
    assert "Foo.bar" in entry.symbol_names
    assert "Foo.baz" in entry.symbol_names
    assert "top_level" in entry.symbol_names
    # Nested helpers are intentionally not indexed.
    assert "top_level.nested" not in entry.symbol_names


def test_python_imports_and_constants() -> None:
    entry = index_source("app/foo.py", PYTHON_SOURCE)
    assert entry.imports == [".sibling", "app.config", "os"]
    constants = [s.name for s in entry.symbols if s.kind is SymbolKind.CONSTANT]
    assert constants == ["TIMEOUT"]


def test_python_symbol_line_ranges_cover_decorators() -> None:
    entry = index_source("app/foo.py", PYTHON_SOURCE)
    baz = next(s for s in entry.symbols if s.name == "Foo.baz")
    lines = PYTHON_SOURCE.splitlines()
    assert lines[baz.start_line - 1].strip() == "@staticmethod"
    assert baz.end_line > baz.start_line


def test_syntax_error_does_not_crash_indexing() -> None:
    entry = index_source("app/broken.py", "def broken(:\n    pass\n")
    assert entry.language == "python"
    assert entry.symbols == []


def test_typescript_extraction() -> None:
    source = (
        "import { helper } from './helper';\n"
        "import React from 'react';\n"
        "export interface Props { id: string }\n"
        "export type Id = string;\n"
        "export class Widget {\n"
        "  render(props: Props) {\n"
        "    return null;\n"
        "  }\n"
        "}\n"
        "export const useThing = async () => {};\n"
        "function plain() {}\n"
    )
    entry = index_source("src/widget.tsx", source)
    assert entry.language == "typescript"
    assert {"Props", "Id", "Widget", "render", "useThing", "plain"} <= set(entry.symbol_names)
    assert entry.imports == ["./helper", "react"]


def test_go_extraction() -> None:
    source = (
        "package main\n\n"
        'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
        "type Server struct {\n\tAddr string\n}\n\n"
        "type Handler interface {\n}\n\n"
        "func (s *Server) Start() error {\n\treturn nil\n}\n\n"
        "func main() {\n\tfmt.Println(1)\n}\n"
    )
    entry = index_source("cmd/main.go", source)
    assert {"Server", "Handler", "Start", "main"} <= set(entry.symbol_names)
    assert entry.imports == ["fmt", "net/http"]


def test_java_extraction() -> None:
    source = (
        "package com.acme;\n"
        "import java.util.List;\n"
        "public class OrderService {\n"
        "    public void placeOrder(String id) {\n"
        "    }\n"
        "}\n"
    )
    entry = index_source("OrderService.java", source)
    assert "OrderService" in entry.symbol_names
    assert "placeOrder" in entry.symbol_names
    assert entry.imports == ["java.util.List"]


def test_rust_extraction() -> None:
    source = (
        "use std::collections::HashMap;\n"
        "pub struct Engine {}\n"
        "pub trait Runner {}\n"
        "impl Engine {\n"
        "    pub fn start(&self) {}\n"
        "}\n"
    )
    entry = index_source("src/lib.rs", source)
    assert {"Engine", "Runner", "start"} <= set(entry.symbol_names)
    assert entry.imports == ["std::collections::HashMap"]


def test_language_detection() -> None:
    assert detect_language("a/b.py") == "python"
    assert detect_language("a/b.tsx") == "typescript"
    assert detect_language("a/b.txt") == "unknown"
    assert is_source_file("main.go")
    assert not is_source_file("README.md")


# ------------------------------------------------------------------- store
def test_store_round_trip(settings) -> None:
    store = RepoMapStore(settings.indexes_dir, 42)
    repo_map = RepoMap(project_id=42, project_name="acme/app")
    repo_map.files["app/foo.py"] = index_source("app/foo.py", PYTHON_SOURCE)
    store.save(repo_map)

    assert store.path.exists()
    loaded = store.load()
    assert loaded.project_id == 42
    assert loaded.files["app/foo.py"].symbol_names == repo_map.files["app/foo.py"].symbol_names
    assert loaded.updated_at is not None


def test_store_load_returns_empty_map_when_absent(settings) -> None:
    assert RepoMapStore(settings.indexes_dir, 99).load().files == {}


def test_store_recovers_from_corrupt_index(settings) -> None:
    store = RepoMapStore(settings.indexes_dir, 7)
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json")
    assert store.load().files == {}


def test_build_full_skips_ignored_and_non_source(settings) -> None:
    store = RepoMapStore(settings.indexes_dir, 1)
    contents = {
        "app/a.py": "def a(): pass\n",
        "docs/readme.md": "# hi\n",
        "node_modules/x/index.js": "module.exports = 1;\n",
    }
    repo_map = store.build_full(
        read_file=contents.get,
        paths=list(contents),
        commit_sha="a" * 40,
        ignore=lambda path: path.startswith("node_modules/"),
    )
    assert list(repo_map.files) == ["app/a.py"]


def test_incremental_update_only_touches_changed_files(settings) -> None:
    store = RepoMapStore(settings.indexes_dir, 1)
    repo_map = RepoMap(project_id=1)
    repo_map.files["app/keep.py"] = index_source("app/keep.py", "def keep(): pass\n")
    repo_map.files["app/gone.py"] = index_source("app/gone.py", "def gone(): pass\n")
    repo_map.files["app/old.py"] = index_source("app/old.py", "def moved(): pass\n")

    reads: list[str] = []

    def read(path: str) -> str | None:
        reads.append(path)
        return {
            "app/changed.py": "def added_symbol(): pass\n",
            "app/new.py": "def moved(): pass\n",
        }.get(path)

    updated = store.apply_changes(
        repo_map,
        read_file=read,
        modified=["app/changed.py"],
        deleted=["app/gone.py"],
        renamed=[("app/old.py", "app/new.py")],
        commit_sha="b" * 40,
    )

    assert set(updated.files) == {"app/keep.py", "app/changed.py", "app/new.py"}
    assert updated.files["app/changed.py"].symbol_names == ["added_symbol"]
    assert updated.commit_sha == "b" * 40
    # The unchanged file was never re-read: this is the incremental guarantee.
    assert "app/keep.py" not in reads


def test_incremental_update_drops_unreadable_file(settings) -> None:
    store = RepoMapStore(settings.indexes_dir, 1)
    repo_map = RepoMap(project_id=1)
    repo_map.files["app/a.py"] = index_source("app/a.py", "def a(): pass\n")
    updated = store.apply_changes(
        repo_map,
        read_file=lambda _p: None,
        modified=["app/a.py"],
        deleted=[],
        renamed=[],
        commit_sha="c" * 40,
    )
    assert updated.files == {}


def test_summary_only_covers_changed_areas() -> None:
    repo_map = RepoMap(project_id=1)
    for path in ("app/routing/a.py", "app/routing/b.py", "unrelated/c.py"):
        repo_map.files[path] = index_source(path, "def thing(): pass\n")

    summary = summarise_repo_map(repo_map, ["app/routing/a.py"])
    assert "app/routing/b.py" in summary
    assert "unrelated/c.py" not in summary
    assert summarise_repo_map(repo_map, []) == ""
