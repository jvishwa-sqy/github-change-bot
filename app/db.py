"""SQLite connection handling and schema management.

SQLite is a deliberate choice: the queue must survive restarts, but the volume
(a handful of pushes per minute at most) never justifies Redis or a broker.
WAL mode plus short IMMEDIATE transactions make concurrent workers safe.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key       TEXT    NOT NULL UNIQUE,
    project_id       INTEGER NOT NULL,
    project_name     TEXT    NOT NULL,
    repo_url         TEXT    NOT NULL,
    ref              TEXT    NOT NULL,
    before_sha       TEXT    NOT NULL,
    after_sha        TEXT    NOT NULL,
    author_name      TEXT    NOT NULL DEFAULT '',
    author_username  TEXT    NOT NULL DEFAULT '',
    commit_count     INTEGER NOT NULL DEFAULT 0,
    payload_json     TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'queued',
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    started_at       TEXT,
    completed_at     TEXT,
    available_at     TEXT    NOT NULL,
    last_error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim   ON jobs (status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs (project_id, created_at);

-- Webhook deliveries already seen, used as a replay guard.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id  TEXT PRIMARY KEY,
    received_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_time ON webhook_deliveries (received_at);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a tuned connection. Callers own closing it."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=timeout,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if absent and record the schema version."""
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a BEGIN IMMEDIATE transaction, committing or rolling back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
