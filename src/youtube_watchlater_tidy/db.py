from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 7

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

CREATE TABLE IF NOT EXISTS metadata_observations (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('found', 'not_found', 'error')),
    exact_match INTEGER NOT NULL DEFAULT 1 CHECK(exact_match IN (0, 1)),
    title TEXT,
    description TEXT,
    channel_id TEXT,
    channel TEXT,
    uploader TEXT,
    uploader_id TEXT,
    duration REAL,
    view_count INTEGER,
    upload_date TEXT,
    timestamp REAL,
    availability TEXT,
    source_url TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metadata_observations_video_source
    ON metadata_observations(video_id, source, id);

CREATE VIEW IF NOT EXISTS preferred_metadata AS
SELECT m.*
FROM metadata_observations AS m
WHERE m.status = 'found'
  AND NOT EXISTS (
      SELECT 1
      FROM metadata_observations AS newer
      WHERE newer.video_id = m.video_id
        AND newer.status = 'found'
        AND newer.id > m.id
  );

CREATE TABLE IF NOT EXISTS archive_lookups (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    backend TEXT NOT NULL,
    looked_up_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('found', 'not_found', 'error')),
    has_video INTEGER NOT NULL DEFAULT 0 CHECK(has_video IN (0, 1)),
    has_metadata INTEGER NOT NULL DEFAULT 0 CHECK(has_metadata IN (0, 1)),
    has_comments INTEGER NOT NULL DEFAULT 0 CHECK(has_comments IN (0, 1)),
    human_verdict TEXT,
    source_url TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archive_lookups_video_backend
    ON archive_lookups(video_id, backend, id);

CREATE TABLE IF NOT EXISTS saved_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    selector_type TEXT NOT NULL CHECK(selector_type IN ('creator', 'title')),
    selector_json TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('keep', 'review', 'archive', 'delete', 'move')),
    destination_playlist TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((action = 'move' AND destination_playlist IS NOT NULL) OR action != 'move')
);

CREATE INDEX IF NOT EXISTS idx_saved_rules_enabled_priority
    ON saved_rules(enabled, priority, id);

CREATE TABLE IF NOT EXISTS dearrow_lookups (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    looked_up_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('found', 'not_found', 'error')),
    preferred_title TEXT,
    titles_json TEXT NOT NULL,
    source_url TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dearrow_lookups_video
    ON dearrow_lookups(video_id, id);

CREATE VIEW IF NOT EXISTS preferred_dearrow AS
SELECT d.*
FROM dearrow_lookups AS d
WHERE d.status = 'found'
  AND d.preferred_title IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM dearrow_lookups AS newer
      WHERE newer.video_id = d.video_id
        AND newer.status = 'found'
        AND newer.preferred_title IS NOT NULL
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

MIGRATION_2_TO_3 = """
CREATE TABLE IF NOT EXISTS metadata_observations (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('found', 'not_found', 'error')),
    exact_match INTEGER NOT NULL DEFAULT 1 CHECK(exact_match IN (0, 1)),
    title TEXT,
    description TEXT,
    channel_id TEXT,
    channel TEXT,
    uploader TEXT,
    uploader_id TEXT,
    duration REAL,
    view_count INTEGER,
    upload_date TEXT,
    timestamp REAL,
    availability TEXT,
    source_url TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metadata_observations_video_source
    ON metadata_observations(video_id, source, id);

CREATE VIEW IF NOT EXISTS preferred_metadata AS
SELECT m.*
FROM metadata_observations AS m
WHERE m.status = 'found'
  AND NOT EXISTS (
      SELECT 1
      FROM metadata_observations AS newer
      WHERE newer.video_id = m.video_id
        AND newer.status = 'found'
        AND newer.id > m.id
  );
"""

MIGRATION_3_TO_4 = """
CREATE TABLE IF NOT EXISTS archive_lookups (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    backend TEXT NOT NULL,
    looked_up_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('found', 'not_found', 'error')),
    has_video INTEGER NOT NULL DEFAULT 0 CHECK(has_video IN (0, 1)),
    has_metadata INTEGER NOT NULL DEFAULT 0 CHECK(has_metadata IN (0, 1)),
    has_comments INTEGER NOT NULL DEFAULT 0 CHECK(has_comments IN (0, 1)),
    human_verdict TEXT,
    source_url TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archive_lookups_video_backend
    ON archive_lookups(video_id, backend, id);
"""

MIGRATION_4_TO_5 = """
CREATE TABLE IF NOT EXISTS saved_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    selector_type TEXT NOT NULL CHECK(selector_type IN ('creator', 'title')),
    selector_json TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('keep', 'review', 'archive', 'delete', 'move')),
    destination_playlist TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((action = 'move' AND destination_playlist IS NOT NULL) OR action != 'move')
);

CREATE INDEX IF NOT EXISTS idx_saved_rules_enabled_priority
    ON saved_rules(enabled, priority, id);
"""

MIGRATION_5_TO_6 = """
CREATE TABLE IF NOT EXISTS dearrow_lookups (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    looked_up_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('found', 'not_found', 'error')),
    preferred_title TEXT,
    titles_json TEXT NOT NULL,
    source_url TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dearrow_lookups_video
    ON dearrow_lookups(video_id, id);

CREATE VIEW IF NOT EXISTS preferred_dearrow AS
SELECT d.*
FROM dearrow_lookups AS d
WHERE d.status = 'found'
  AND d.preferred_title IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM dearrow_lookups AS newer
      WHERE newer.video_id = d.video_id
        AND newer.status = 'found'
        AND newer.preferred_title IS NOT NULL
        AND newer.id > d.id
  );
"""

MIGRATION_6_TO_7 = """
CREATE TABLE IF NOT EXISTS llm_classification_runs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    selection_id INTEGER REFERENCES selections(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete', 'error')),
    provider_name TEXT NOT NULL,
    provider_preset TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    provider_sha256 TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    interest_profile TEXT,
    video_count INTEGER NOT NULL,
    provider_config_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_runs_cache
    ON llm_classification_runs(
        snapshot_id, provider_sha256, prompt_sha256, input_sha256, status, id
    );
CREATE INDEX IF NOT EXISTS idx_llm_runs_snapshot
    ON llm_classification_runs(snapshot_id, id);

CREATE TABLE IF NOT EXISTS llm_classification_batches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES llm_classification_runs(id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL,
    response_model TEXT,
    usage_json TEXT NOT NULL,
    raw_response_json TEXT NOT NULL,
    validated_json TEXT NOT NULL,
    UNIQUE(run_id, batch_index)
);

CREATE TABLE IF NOT EXISTS llm_classifications (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES llm_classification_runs(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES llm_classification_batches(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    playlist_position INTEGER NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('keep', 'review', 'archive', 'delete', 'move')),
    topic TEXT NOT NULL,
    content_type TEXT NOT NULL,
    timeliness TEXT NOT NULL CHECK(timeliness IN ('evergreen', 'current', 'stale', 'unknown')),
    quality REAL NOT NULL CHECK(quality >= 0 AND quality <= 1),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    reason TEXT NOT NULL,
    existing_playlist TEXT,
    new_queue_proposal TEXT,
    destination_confidence REAL NOT NULL CHECK(destination_confidence >= 0 AND destination_confidence <= 1),
    destination_reason TEXT NOT NULL,
    needs_description INTEGER NOT NULL CHECK(needs_description IN (0, 1)),
    needs_transcript INTEGER NOT NULL CHECK(needs_transcript IN (0, 1)),
    raw_result_json TEXT NOT NULL,
    UNIQUE(run_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_llm_classifications_video
    ON llm_classifications(video_id, id);
CREATE INDEX IF NOT EXISTS idx_llm_classifications_run
    ON llm_classifications(run_id, playlist_position, id);
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
            conn.executescript(MIGRATION_6_TO_7)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    if version == 1:
        with conn:
            conn.executescript(MIGRATION_1_TO_2)
            conn.executescript(MIGRATION_2_TO_3)
            conn.executescript(MIGRATION_3_TO_4)
            conn.executescript(MIGRATION_4_TO_5)
            conn.executescript(MIGRATION_5_TO_6)
            conn.executescript(MIGRATION_6_TO_7)
            conn.execute("PRAGMA user_version = 7")
        return

    if version == 2:
        with conn:
            conn.executescript(MIGRATION_2_TO_3)
            conn.executescript(MIGRATION_3_TO_4)
            conn.executescript(MIGRATION_4_TO_5)
            conn.executescript(MIGRATION_5_TO_6)
            conn.executescript(MIGRATION_6_TO_7)
            conn.execute("PRAGMA user_version = 7")
        return

    if version == 3:
        with conn:
            conn.executescript(MIGRATION_3_TO_4)
            conn.executescript(MIGRATION_4_TO_5)
            conn.executescript(MIGRATION_5_TO_6)
            conn.executescript(MIGRATION_6_TO_7)
            conn.execute("PRAGMA user_version = 7")
        return

    if version == 4:
        with conn:
            conn.executescript(MIGRATION_4_TO_5)
            conn.executescript(MIGRATION_5_TO_6)
            conn.executescript(MIGRATION_6_TO_7)
            conn.execute("PRAGMA user_version = 7")
        return

    if version == 5:
        with conn:
            conn.executescript(MIGRATION_5_TO_6)
            conn.executescript(MIGRATION_6_TO_7)
            conn.execute("PRAGMA user_version = 7")
        return

    if version == 6:
        with conn:
            conn.executescript(MIGRATION_6_TO_7)
            conn.execute("PRAGMA user_version = 7")
        return

    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is not supported; expected {SCHEMA_VERSION}"
        )
