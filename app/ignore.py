"""Path ignore matching.

Gitignore-style globbing without a dependency. Patterns are anchored at any
directory depth unless they start with "/", so ``node_modules/**`` also matches
``frontend/node_modules/react/index.js``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from functools import lru_cache


def _translate(pattern: str) -> str:
    """Convert one glob pattern into a full-match regex."""
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")

    out: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char == "*":
            if body.startswith("**", index):
                # "**/" spans zero or more directories; bare "**" spans anything.
                if body.startswith("**/", index):
                    out.append("(?:.*/)?")
                    index += 3
                    continue
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
        elif char == "/":
            out.append("/")
        else:
            out.append(re.escape(char))
        index += 1

    regex = "".join(out)
    if body.endswith("/"):  # "build/" means the directory and everything in it
        regex += ".*"
    if not anchored:
        regex = "(?:.*/)?" + regex
    return f"(?:{regex})\\Z"


@lru_cache(maxsize=64)
def _compile(patterns: tuple[str, ...]) -> re.Pattern[str] | None:
    cleaned = [p.strip() for p in patterns if p.strip() and not p.strip().startswith("#")]
    if not cleaned:
        return None
    return re.compile("|".join(_translate(p) for p in cleaned))


class IgnoreMatcher:
    """Decides whether a repository path is worth analysing."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = tuple(patterns)
        self._regex = _compile(self.patterns)

    def __call__(self, path: str) -> bool:
        return self.matches(path)

    def matches(self, path: str) -> bool:
        if self._regex is None:
            return False
        return self._regex.match(path.lstrip("./")) is not None

    def filter(self, paths: Iterable[str]) -> list[str]:
        """Return only the paths that are *not* ignored."""
        return [p for p in paths if not self.matches(p)]


def make_matcher(patterns: Iterable[str]) -> Callable[[str], bool]:
    return IgnoreMatcher(patterns)
