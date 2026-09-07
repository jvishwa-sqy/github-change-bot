"""Deterministic repository map.

The map is what gives the LLM cheap structural awareness of a repository it
never sees in full. It is built with the standard library only — never by
feeding source code to a model — and updated incrementally: after a push only
the changed files are re-parsed.

Extractors are registered per language behind a small protocol, so a
Tree-sitter backend can replace the lightweight ones later without touching
callers. v1 deliberately stays dependency-free.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.models import FileIndex, RepoMap, Symbol, SymbolKind

log = logging.getLogger(__name__)

# Files above this size are indexed as "present" but not parsed.
MAX_PARSE_BYTES = 1_000_000

EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
}


def detect_language(path: str) -> str:
    return EXTENSION_LANGUAGES.get(Path(path).suffix.lower(), "unknown")


def is_source_file(path: str) -> bool:
    return detect_language(path) != "unknown"


class SymbolExtractor(Protocol):
    """Extracts symbols and imports from a single file's source text."""

    language: str

    def extract(self, source: str) -> tuple[list[Symbol], list[str]]: ...


# --------------------------------------------------------------------- Python
class PythonExtractor:
    """Exact extraction via the standard-library AST."""

    language = "python"

    def extract(self, source: str) -> tuple[list[Symbol], list[str]]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # A syntactically broken file is still worth listing; just no symbols.
            return [], []

        symbols: list[Symbol] = []
        imports: list[str] = []

        def start_of(node: ast.AST) -> int:
            """Definition start, including decorators, so context stays complete."""
            decorators = getattr(node, "decorator_list", [])
            lines = [getattr(node, "lineno", 1)]
            lines += [d.lineno for d in decorators if hasattr(d, "lineno")]
            return min(lines)

        def visit(node: ast.AST, prefix: str = "") -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    name = f"{prefix}{child.name}"
                    symbols.append(
                        Symbol(
                            name=name,
                            kind=SymbolKind.CLASS,
                            start_line=start_of(child),
                            end_line=child.end_lineno or child.lineno,
                        )
                    )
                    visit(child, prefix=f"{name}.")
                elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    symbols.append(
                        Symbol(
                            name=f"{prefix}{child.name}",
                            kind=SymbolKind.METHOD if prefix else SymbolKind.FUNCTION,
                            start_line=start_of(child),
                            end_line=child.end_lineno or child.lineno,
                        )
                    )
                    # Nested helpers are rarely useful context on their own.
                elif isinstance(child, ast.Import):
                    imports.extend(alias.name for alias in child.names)
                elif isinstance(child, ast.ImportFrom):
                    dots = "." * (child.level or 0)
                    if child.module:
                        imports.append(dots + child.module)
                    elif dots:
                        # "from . import sibling" — the name is the module.
                        imports.extend(dots + alias.name for alias in child.names)
                elif isinstance(child, ast.Assign) and not prefix:
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id.isupper():
                            symbols.append(
                                Symbol(
                                    name=target.id,
                                    kind=SymbolKind.CONSTANT,
                                    start_line=child.lineno,
                                    end_line=child.end_lineno or child.lineno,
                                )
                            )
                elif isinstance(child, ast.If | ast.Try | ast.With):
                    visit(child, prefix)  # module-level conditional definitions

        visit(tree)
        return symbols, sorted(set(imports))


# ------------------------------------------------------- regex-based (v1)
class RegexExtractor:
    """Deterministic best-effort extraction for non-Python languages.

    Accuracy is intentionally traded for zero dependencies: the map only needs
    to name the things in a file, while the context builder falls back to a
    bounded line window for these languages.
    """

    def __init__(
        self,
        language: str,
        symbol_patterns: list[tuple[re.Pattern[str], SymbolKind]],
        import_patterns: list[re.Pattern[str]],
    ) -> None:
        self.language = language
        self._symbol_patterns = symbol_patterns
        self._import_patterns = import_patterns

    def extract(self, source: str) -> tuple[list[Symbol], list[str]]:
        lines = source.splitlines()
        found: list[Symbol] = []
        imports: list[str] = []

        for number, line in enumerate(lines, start=1):
            if len(line) > 500:  # minified or generated; skip
                continue
            for pattern in self._import_patterns:
                if match := pattern.search(line):
                    imports.append(match.group(1))
            for pattern, kind in self._symbol_patterns:
                if match := pattern.search(line):
                    found.append(
                        Symbol(name=match.group(1), kind=kind, start_line=number, end_line=number)
                    )
                    break

        # End line is approximated as "up to the next definition", which is
        # enough for the map; exact bodies come from the diff itself.
        for index, symbol in enumerate(found):
            next_start = found[index + 1].start_line - 1 if index + 1 < len(found) else len(lines)
            symbol.end_line = max(symbol.start_line, next_start)

        return found, sorted(set(imports))


_JS_SYMBOLS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"), SymbolKind.CLASS),
    (
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"
        ),
        SymbolKind.FUNCTION,
    ),
    (
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
            r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
        ),
        SymbolKind.FUNCTION,
    ),
    (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"), SymbolKind.INTERFACE),
    (re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*="), SymbolKind.TYPE),
    (
        re.compile(
            r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*\{"
        ),
        SymbolKind.METHOD,
    ),
]
_JS_IMPORTS = [
    re.compile(r"""^\s*import\s+(?:.+?\s+from\s+)?['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""^\s*export\s+.*\bfrom\s+['"]([^'"]+)['"]"""),
]

_JAVA_SYMBOLS = [
    (
        re.compile(r"^\s*(?:public|private|protected|abstract|final|static|\s)*class\s+(\w+)"),
        SymbolKind.CLASS,
    ),
    (re.compile(r"^\s*(?:public|private|protected|\s)*interface\s+(\w+)"), SymbolKind.INTERFACE),
    (re.compile(r"^\s*(?:public|private|protected|\s)*enum\s+(\w+)"), SymbolKind.TYPE),
    (
        re.compile(
            r"^\s+(?:public|private|protected|static|final|synchronized|abstract|native|\s)+"
            r"[\w<>\[\],.?\s]+\s+(\w+)\s*\([^)]*\)\s*(?:throws [\w,.\s]+)?\{"
        ),
        SymbolKind.METHOD,
    ),
]
_JAVA_IMPORTS = [re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;")]

_GO_SYMBOLS = [
    (re.compile(r"^func\s+\([^)]*\)\s*(\w+)\s*\("), SymbolKind.METHOD),
    (re.compile(r"^func\s+(\w+)\s*[\(\[]"), SymbolKind.FUNCTION),
    (re.compile(r"^type\s+(\w+)\s+struct"), SymbolKind.STRUCT),
    (re.compile(r"^type\s+(\w+)\s+interface"), SymbolKind.INTERFACE),
    (re.compile(r"^type\s+(\w+)\s+"), SymbolKind.TYPE),
]
_GO_IMPORTS = [
    re.compile(r"""^\s*(?:import\s+)?(?:[\w.]+\s+)?"([\w./\-]+)"\s*$"""),
]

_RUST_SYMBOLS = [
    (
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)"),
        SymbolKind.FUNCTION,
    ),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+(\w+)"), SymbolKind.STRUCT),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+(\w+)"), SymbolKind.TYPE),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)"), SymbolKind.INTERFACE),
    (re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([\w:]+)"), SymbolKind.CLASS),
]
_RUST_IMPORTS = [re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)")]

_EXTRACTORS: dict[str, SymbolExtractor] = {
    "python": PythonExtractor(),
    "javascript": RegexExtractor("javascript", _JS_SYMBOLS, _JS_IMPORTS),
    "typescript": RegexExtractor("typescript", _JS_SYMBOLS, _JS_IMPORTS),
    "java": RegexExtractor("java", _JAVA_SYMBOLS, _JAVA_IMPORTS),
    "go": RegexExtractor("go", _GO_SYMBOLS, _GO_IMPORTS),
    "rust": RegexExtractor("rust", _RUST_SYMBOLS, _RUST_IMPORTS),
}


def get_extractor(language: str) -> SymbolExtractor | None:
    return _EXTRACTORS.get(language)


def register_extractor(extractor: SymbolExtractor) -> None:
    """Hook for a future Tree-sitter backend to override a language."""
    _EXTRACTORS[extractor.language] = extractor


def index_source(path: str, source: str) -> FileIndex:
    """Build the map entry for one file."""
    language = detect_language(path)
    extractor = get_extractor(language)
    symbols: list[Symbol] = []
    imports: list[str] = []
    if extractor is not None and len(source) <= MAX_PARSE_BYTES:
        symbols, imports = extractor.extract(source)
    return FileIndex(
        path=path,
        language=language,
        symbols=symbols,
        imports=imports,
        loc=source.count("\n") + 1 if source else 0,
    )


class RepoMapStore:
    """Loads, updates and persists one project's repo map as JSON."""

    def __init__(self, indexes_dir: Path, project_id: int) -> None:
        self.project_id = project_id
        self.dir = indexes_dir / str(project_id)
        self.path = self.dir / "repo-map.json"

    # ------------------------------------------------------------ persistence
    def load(self) -> RepoMap:
        if not self.path.exists():
            return RepoMap(project_id=self.project_id)
        try:
            return RepoMap.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning(
                "repo map unreadable, rebuilding from empty",
                extra={"project_id": self.project_id, "error": str(exc)[:200]},
            )
            return RepoMap(project_id=self.project_id)

    def save(self, repo_map: RepoMap) -> None:
        """Atomic write so a crash can never leave a half-written index."""
        self.dir.mkdir(parents=True, exist_ok=True)
        repo_map.updated_at = datetime.now(UTC)
        payload = repo_map.model_dump_json(indent=None)
        fd, tmp_name = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    # ---------------------------------------------------------------- builds
    def build_full(
        self,
        *,
        read_file,  # Callable[[str], str | None]
        paths: Iterable[str],
        commit_sha: str,
        project_name: str = "",
        ignore=lambda _p: False,  # Callable[[str], bool]
    ) -> RepoMap:
        """Full index. Used only by bootstrap — never on the push path."""
        repo_map = RepoMap(
            project_id=self.project_id, project_name=project_name, commit_sha=commit_sha
        )
        for path in paths:
            if not is_source_file(path) or ignore(path):
                continue
            source = read_file(path)
            if source is None:
                continue
            repo_map.files[path] = index_source(path, source)
        return repo_map

    def apply_changes(
        self,
        repo_map: RepoMap,
        *,
        read_file,  # Callable[[str], str | None]
        modified: Iterable[str],
        deleted: Iterable[str],
        renamed: Iterable[tuple[str, str]],
        commit_sha: str,
    ) -> RepoMap:
        """Incremental update: only touched paths are re-parsed.

        This is what keeps per-push work proportional to the change rather
        than to repository size.
        """
        for old_path, new_path in renamed:
            repo_map.files.pop(old_path, None)
            if not is_source_file(new_path):
                continue
            if (source := read_file(new_path)) is not None:
                repo_map.files[new_path] = index_source(new_path, source)

        for path in deleted:
            repo_map.files.pop(path, None)

        for path in modified:
            if not is_source_file(path):
                continue
            source = read_file(path)
            if source is None:
                repo_map.files.pop(path, None)
                continue
            repo_map.files[path] = index_source(path, source)

        repo_map.commit_sha = commit_sha
        return repo_map


def summarise_repo_map(repo_map: RepoMap, focus_paths: Iterable[str], *, limit: int = 30) -> str:
    """Compact textual overview of the areas a change touches.

    Only directories involved in the change are described, which keeps the
    prompt small no matter how large the repository is.
    """
    focus = list(focus_paths)
    if not repo_map.files or not focus:
        return ""

    focus_dirs = {str(Path(p).parent) for p in focus}
    lines: list[str] = []
    for path, entry in sorted(repo_map.files.items()):
        if str(Path(path).parent) not in focus_dirs or path in focus:
            continue
        names = entry.symbol_names[:6]
        if names:
            lines.append(f"- {path} ({entry.language}): {', '.join(names)}")
        if len(lines) >= limit:
            break

    if not lines:
        return ""
    header = (
        f"Repository map (deterministically indexed: {repo_map.file_count} files, "
        f"{repo_map.symbol_count} symbols). Neighbouring files in the changed areas:"
    )
    return header + "\n" + "\n".join(lines)
