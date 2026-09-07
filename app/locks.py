"""Per-project advisory file locks.

Git operations on a shared mirror must not run concurrently. One worker is the
default deployment, but the lock makes multi-worker safe without extra
infrastructure.
"""

from __future__ import annotations

import fcntl
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


@contextmanager
def project_lock(locks_dir: Path, project_id: int, *, blocking: bool = True) -> Iterator[bool]:
    """Hold an exclusive lock for `project_id`.

    Yields True when the lock was acquired. With blocking=False it yields
    False immediately if another process holds it.
    """
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{project_id}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError:
            log.debug("project lock busy", extra={"project_id": project_id})
            yield False
            return
        yield True
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
