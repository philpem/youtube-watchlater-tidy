from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

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
    elif version != SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is not supported; expected {SCHEMA_VERSION}"
        )
