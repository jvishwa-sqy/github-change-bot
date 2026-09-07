#!/usr/bin/env python3
"""Bootstrap a repository: create its mirror and build the initial repo map.

    python scripts/bootstrap_repo.py \
        --project-id 123 \
        --repo-url git@github.com:team/project.git \
        --ref main

No LLM is involved: the map is built deterministically from the source tree
read straight out of the bare mirror via git plumbing (no working copy is ever
checked out). Run this once per repository before enabling its webhook so the
first push already has full structural context.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

# Allow running the script directly from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings_lenient  # noqa: E402
from app.errors import ChangeBotError  # noqa: E402
from app.git_repo import GitConfig, GitMirror, sanitize_url  # noqa: E402
from app.ignore import IgnoreMatcher  # noqa: E402
from app.locks import project_lock  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.repo_map import RepoMapStore, is_source_file  # noqa: E402

log = logging.getLogger("bootstrap")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", type=int, required=True, help="GitHub repository id")
    parser.add_argument("--repo-url", required=True, help="SSH or HTTPS clone URL")
    parser.add_argument("--ref", default="main", help="Branch to index (default: main)")
    parser.add_argument(
        "--project-name", default="", help="Display name, e.g. owner/repo (optional)"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="Override BOT_DATA_DIR for this run"
    )
    parser.add_argument(
        "--force-refetch", action="store_true", help="Fetch even if the mirror already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings_lenient()
    if args.data_dir:
        settings = settings.model_copy(update={"bot_data_dir": args.data_dir})
    configure_logging(settings.log_level)
    settings.ensure_directories()

    git_config = GitConfig(
        repos_dir=settings.repos_dir,
        ssh_command=settings.git_ssh_command,
        timeout_seconds=settings.git_timeout_seconds,
        token=settings.github_token.get_secret_value() if settings.github_token else None,
    )
    mirror = GitMirror(args.project_id, args.repo_url, git_config)
    ignore = IgnoreMatcher(settings.all_ignore_patterns)

    started = time.monotonic()
    print(f"Repository : {sanitize_url(args.repo_url)}")
    print(f"Project id : {args.project_id}")
    print(f"Mirror     : {mirror.path}")

    with project_lock(settings.locks_dir, args.project_id):
        existed = mirror.exists
        mirror.ensure()
        if existed or args.force_refetch:
            print("Fetching latest objects…")
            mirror.fetch()
        else:
            print("Mirror created.")

        commit = mirror.rev_parse(args.ref) or mirror.rev_parse(f"refs/heads/{args.ref}")
        if commit is None:
            print(f"error: ref {args.ref!r} not found in the mirror", file=sys.stderr)
            return 2
        print(f"Ref        : {args.ref} → {commit[:12]}")

        paths = mirror.list_files(commit)
        store = RepoMapStore(settings.indexes_dir, args.project_id)
        repo_map = store.build_full(
            read_file=lambda path: mirror.file_content(commit, path),
            paths=paths,
            commit_sha=commit,
            project_name=args.project_name,
            ignore=ignore,
        )
        store.save(repo_map)

    languages = Counter(entry.language for entry in repo_map.files.values())
    skipped = sum(1 for p in paths if is_source_file(p) and ignore.matches(p))
    elapsed = time.monotonic() - started

    print()
    print("Repository map written to", store.path)
    print(f"  tracked files      : {len(paths)}")
    print(f"  indexed files      : {repo_map.file_count}")
    print(f"  ignored source     : {skipped}")
    print(f"  symbols            : {repo_map.symbol_count}")
    print(f"  lines of code      : {sum(e.loc for e in repo_map.files.values())}")
    print("  languages          :")
    for language, count in languages.most_common():
        print(f"      {language:<12} {count}")
    print(f"  elapsed            : {elapsed:.1f}s")
    print()
    print("No LLM calls were made.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ChangeBotError as error:  # pragma: no cover - operator-facing
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
