from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL,
    playlist_id TEXT,
    playlist_title TEXT,
    playlist_modified_date TEXT,
    reported_playlist_count INTEGER,
    entry_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_entries (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    title TEXT,
    description TEXT,
    channel_id TEXT,
    channel TEXT,
    uploader TEXT,
    uploader_id TEXT,
    duration REAL,
    view_count INTEGER,
    availability TEXT,
    timestamp REAL,
    release_timestamp REAL,
    thumbnails_json TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, position),
    UNIQUE (snapshot_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_entries_video_id
    ON snapshot_entries(video_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_entries_channel_id
    ON snapshot_entries(snapshot_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_entries_channel
    ON snapshot_entries(snapshot_id, channel);

CREATE TABLE IF NOT EXISTS selections (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    selector_type TEXT NOT NULL,
    selector_json TEXT NOT NULL,
    entry_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS selection_entries (
    selection_id INTEGER NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    PRIMARY KEY (selection_id, video_id)
);

CREATE TABLE IF NOT EXISTS decision_events (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    action TEXT NOT NULL CHECK(action IN ('keep', 'review', 'archive', 'delete', 'move', 'clear')),
    destination_playlist TEXT,
    source TEXT NOT NULL,
    rule_json TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES decision_events(id),
    CHECK((action = 'move' AND destination_playlist IS NOT NULL) OR action != 'move')
);

CREATE INDEX IF NOT EXISTS idx_decision_events_snapshot_video
    ON decision_events(snapshot_id, video_id, id);
CREATE INDEX IF NOT EXISTS idx_selections_snapshot
    ON selections(snapshot_id, id);

CREATE VIEW IF NOT EXISTS current_decisions AS
SELECT d.*
FROM decision_events AS d
WHERE NOT EXISTS (
    SELECT 1
    FROM decision_events AS newer
    WHERE newer.snapshot_id = d.snapshot_id
      AND newer.video_id = d.video_id
      AND newer.id > d.id
);
"""

MIGRATION_1_TO_2 = """
CREATE TABLE IF NOT EXISTS selections (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    selector_type TEXT NOT NULL,
    selector_json TEXT NOT NULL,
    entry_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS selection_entries (
    selection_id INTEGER NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    PRIMARY KEY (selection_id, video_id)
);

CREATE TABLE IF NOT EXISTS decision_events (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    action TEXT NOT NULL CHECK(action IN ('keep', 'review', 'archive', 'delete', 'move', 'clear')),
    destination_playlist TEXT,
    source TEXT NOT NULL,
    rule_json TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES decision_events(id),
    CHECK((action = 'move' AND destination_playlist IS NOT NULL) OR action != 'move')
);

CREATE INDEX IF NOT EXISTS idx_decision_events_snapshot_video
    ON decision_events(snapshot_id, video_id, id);
CREATE INDEX IF NOT EXISTS idx_selections_snapshot
    ON selections(snapshot_id, id);

CREATE VIEW IF NOT EXISTS current_decisions AS
SELECT d.*
FROM decision_events AS d
WHERE NOT EXISTS (
    SELECT 1
    FROM decision_events AS newer
    WHERE newer.snapshot_id = d.snapshot_id
      AND newer.video_id = d.video_id
      AND newer.id > d.id
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if db_path.parent != Path(""):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


@contextmanager
def open_catalogue(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than this program supports "
            f"({SCHEMA_VERSION})"
        )

    if version == 0:
        with conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    if version == 1:
        with conn:
            conn.executescript(MIGRATION_1_TO_2)
            conn.execute("PRAGMA user_version = 2")
        return

    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is not supported; expected {SCHEMA_VERSION}"
        )
