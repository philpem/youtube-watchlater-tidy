from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .reports import latest_snapshot_id

INVENTORY_FORMAT = "youtube-watchlater-tidy-playlist-inventory-v1"
DEFAULT_PLAYLIST_CREATE_COST = 50
DEFAULT_PLAYLIST_INSERT_COST = 50
DEFAULT_API_QUOTA_LIMIT = 10_000
PRIVACY_VALUES = {"private", "unlisted", "public", "unknown"}
BACKENDS = {"api", "browser"}

PLAYLIST_SYNC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS youtube_playlist_inventory_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    fetched_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    source TEXT NOT NULL,
    format TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_playlist_inventory (
    playlist_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    privacy_status TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_youtube_playlist_inventory_title
    ON youtube_playlist_inventory(title);

CREATE TABLE IF NOT EXISTS youtube_playlist_inventory_items (
    playlist_id TEXT NOT NULL REFERENCES youtube_playlist_inventory(playlist_id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    playlist_item_id TEXT,
    position INTEGER,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (playlist_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_youtube_playlist_inventory_items_video
    ON youtube_playlist_inventory_items(video_id, playlist_id);

CREATE TABLE IF NOT EXISTS playlist_sync_runs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    backend TEXT NOT NULL CHECK(backend IN ('api', 'browser')),
    status TEXT NOT NULL CHECK(status IN ('planned', 'running', 'partial', 'complete', 'cancelled')),
    inventory_fetched_at TEXT NOT NULL,
    new_playlist_privacy TEXT NOT NULL CHECK(new_playlist_privacy IN ('private', 'unlisted', 'public')),
    playlist_create_cost INTEGER NOT NULL,
    playlist_insert_cost INTEGER NOT NULL,
    quota_limit INTEGER NOT NULL,
    estimated_quota INTEGER NOT NULL,
    exceeds_quota INTEGER NOT NULL CHECK(exceeds_quota IN (0, 1)),
    plan_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_playlist_sync_runs_snapshot
    ON playlist_sync_runs(snapshot_id, id);

CREATE TABLE IF NOT EXISTS playlist_sync_destinations (
    run_id INTEGER NOT NULL REFERENCES playlist_sync_runs(id) ON DELETE CASCADE,
    destination_name TEXT NOT NULL,
    destination_playlist_id TEXT,
    privacy_status TEXT NOT NULL CHECK(privacy_status IN ('private', 'unlisted', 'public', 'unknown')),
    status TEXT NOT NULL CHECK(status IN ('existing', 'create_planned', 'created', 'failed')),
    created_at TEXT,
    error TEXT,
    PRIMARY KEY (run_id, destination_name)
);

CREATE TABLE IF NOT EXISTS playlist_sync_items (
    run_id INTEGER NOT NULL REFERENCES playlist_sync_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    decision_event_id INTEGER NOT NULL REFERENCES decision_events(id),
    destination_name TEXT NOT NULL,
    destination_playlist_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('planned', 'already_present', 'inserted', 'failed', 'skipped')),
    playlist_item_id TEXT,
    attempted_at TEXT,
    completed_at TEXT,
    error TEXT,
    PRIMARY KEY (run_id, ordinal),
    UNIQUE (run_id, video_id, destination_name)
);

CREATE INDEX IF NOT EXISTS idx_playlist_sync_items_run_status
    ON playlist_sync_items(run_id, status, ordinal);
"""


@dataclass(frozen=True)
class InventoryImportResult:
    playlists: int
    items: int
    fetched_at: str
    source: str


@dataclass(frozen=True)
class PlaylistPlan:
    run_id: int
    snapshot_id: int
    backend: str
    destination_count: int
    create_count: int
    insert_count: int
    already_present_count: int
    estimated_quota: int
    quota_limit: int
    exceeds_quota: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_playlist_sync_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(PLAYLIST_SYNC_SCHEMA_SQL)


def _privacy(value: Any, field: str, *, allow_unknown: bool = True) -> str:
    result = str(value or "unknown").casefold()
    allowed = PRIVACY_VALUES if allow_unknown else PRIVACY_VALUES - {"unknown"}
    if result not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(sorted(allowed))}")
    return result


def load_inventory_file(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format") != INVENTORY_FORMAT:
        raise ValueError(f"playlist inventory must use format {INVENTORY_FORMAT!r}")
    playlists = value.get("playlists")
    if not isinstance(playlists, list):
        raise ValueError("playlist inventory 'playlists' must be an array")
    source = value.get("source", "manual")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("playlist inventory source must be a non-empty string")
    fetched_at = value.get("fetched_at") or _utc_now()
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise ValueError("playlist inventory fetched_at must be a string")

    seen_ids: set[str] = set()
    for index, row in enumerate(playlists):
        if not isinstance(row, dict):
            raise ValueError(f"playlists[{index}] must be an object")
        playlist_id = row.get("playlist_id")
        title = row.get("title")
        if not isinstance(playlist_id, str) or not playlist_id.strip():
            raise ValueError(f"playlists[{index}].playlist_id must be a non-empty string")
        if playlist_id in seen_ids:
            raise ValueError(f"duplicate playlist_id {playlist_id!r}")
        seen_ids.add(playlist_id)
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"playlists[{index}].title must be a non-empty string")
        _privacy(row.get("privacy_status", "unknown"), f"playlists[{index}].privacy_status")
        items = row.get("items", [])
        if not isinstance(items, list):
            raise ValueError(f"playlists[{index}].items must be an array")
        seen_videos: set[str] = set()
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"playlists[{index}].items[{item_index}] must be an object")
            video_id = item.get("video_id")
            if not isinstance(video_id, str) or not video_id.strip():
                raise ValueError(
                    f"playlists[{index}].items[{item_index}].video_id must be a non-empty string"
                )
            if video_id in seen_videos:
                raise ValueError(
                    f"duplicate video_id {video_id!r} inside playlist {playlist_id!r}"
                )
            seen_videos.add(video_id)
            playlist_item_id = item.get("playlist_item_id")
            if playlist_item_id is not None and not isinstance(playlist_item_id, str):
                raise ValueError(
                    f"playlists[{index}].items[{item_index}].playlist_item_id must be a string or null"
                )
            position = item.get("position")
            if position is not None and (isinstance(position, bool) or not isinstance(position, int)):
                raise ValueError(
                    f"playlists[{index}].items[{item_index}].position must be an integer or null"
                )
    return value


def import_inventory(conn: sqlite3.Connection, payload: dict[str, Any]) -> InventoryImportResult:
    ensure_playlist_sync_schema(conn)
    # Revalidate programmatic callers through the same structural rules used for files.
    playlists = payload.get("playlists")
    if payload.get("format") != INVENTORY_FORMAT or not isinstance(playlists, list):
        raise ValueError(f"playlist inventory must use format {INVENTORY_FORMAT!r}")
    source = payload.get("source", "manual")
    fetched_at = payload.get("fetched_at") or _utc_now()
    if not isinstance(source, str) or not source.strip():
        raise ValueError("playlist inventory source must be a non-empty string")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise ValueError("playlist inventory fetched_at must be a string")

    # Validate by serializing through an in-memory equivalent of load_inventory_file's rules.
    seen_ids: set[str] = set()
    item_count = 0
    normalized: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for index, row in enumerate(playlists):
        if not isinstance(row, dict):
            raise ValueError(f"playlists[{index}] must be an object")
        playlist_id = row.get("playlist_id")
        title = row.get("title")
        if not isinstance(playlist_id, str) or not playlist_id.strip():
            raise ValueError(f"playlists[{index}].playlist_id must be a non-empty string")
        if playlist_id in seen_ids:
            raise ValueError(f"duplicate playlist_id {playlist_id!r}")
        seen_ids.add(playlist_id)
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"playlists[{index}].title must be a non-empty string")
        privacy = _privacy(row.get("privacy_status", "unknown"), f"playlists[{index}].privacy_status")
        items = row.get("items", [])
        if not isinstance(items, list):
            raise ValueError(f"playlists[{index}].items must be an array")
        seen_videos: set[str] = set()
        normalized_items: list[dict[str, Any]] = []
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"playlists[{index}].items[{item_index}] must be an object")
            video_id = item.get("video_id")
            if not isinstance(video_id, str) or not video_id.strip():
                raise ValueError(
                    f"playlists[{index}].items[{item_index}].video_id must be a non-empty string"
                )
            if video_id in seen_videos:
                raise ValueError(f"duplicate video_id {video_id!r} inside playlist {playlist_id!r}")
            seen_videos.add(video_id)
            playlist_item_id = item.get("playlist_item_id")
            if playlist_item_id is not None and not isinstance(playlist_item_id, str):
                raise ValueError(
                    f"playlists[{index}].items[{item_index}].playlist_item_id must be a string or null"
                )
            position = item.get("position")
            if position is not None and (isinstance(position, bool) or not isinstance(position, int)):
                raise ValueError(
                    f"playlists[{index}].items[{item_index}].position must be an integer or null"
                )
            normalized_items.append(item)
            item_count += 1
        normalized.append(({
            **row,
            "playlist_id": playlist_id,
            "title": title,
            "privacy_status": privacy,
        }, normalized_items))

    imported_at = _utc_now()
    with conn:
        conn.execute("DELETE FROM youtube_playlist_inventory_items")
        conn.execute("DELETE FROM youtube_playlist_inventory")
        conn.execute("DELETE FROM youtube_playlist_inventory_meta")
        conn.execute(
            """
            INSERT INTO youtube_playlist_inventory_meta (
                singleton, fetched_at, imported_at, source, format, raw_json
            ) VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                fetched_at,
                imported_at,
                source,
                INVENTORY_FORMAT,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        for row, items in normalized:
            conn.execute(
                """
                INSERT INTO youtube_playlist_inventory (
                    playlist_id, title, privacy_status, fetched_at, source, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["playlist_id"],
                    row["title"],
                    row["privacy_status"],
                    fetched_at,
                    source,
                    json.dumps(row, ensure_ascii=False, sort_keys=True),
                ),
            )
            for item in items:
                conn.execute(
                    """
                    INSERT INTO youtube_playlist_inventory_items (
                        playlist_id, video_id, playlist_item_id, position, raw_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["playlist_id"],
                        item["video_id"],
                        item.get("playlist_item_id"),
                        item.get("position"),
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                    ),
                )
    return InventoryImportResult(
        playlists=len(normalized),
        items=item_count,
        fetched_at=str(fetched_at),
        source=source,
    )


def inventory_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_playlist_sync_schema(conn)
    meta = conn.execute("SELECT * FROM youtube_playlist_inventory_meta WHERE singleton = 1").fetchone()
    if meta is None:
        raise ValueError("no playlist inventory has been imported")
    playlists = conn.execute(
        "SELECT * FROM youtube_playlist_inventory ORDER BY title COLLATE NOCASE, playlist_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for playlist in playlists:
        items = conn.execute(
            """
            SELECT video_id, playlist_item_id, position
            FROM youtube_playlist_inventory_items
            WHERE playlist_id = ?
            ORDER BY COALESCE(position, 2147483647), video_id
            """,
            (playlist["playlist_id"],),
        ).fetchall()
        result.append(
            {
                "playlist_id": playlist["playlist_id"],
                "title": playlist["title"],
                "privacy_status": playlist["privacy_status"],
                "items": [dict(row) for row in items],
            }
        )
    return {
        "format": meta["format"],
        "source": meta["source"],
        "fetched_at": meta["fetched_at"],
        "imported_at": meta["imported_at"],
        "playlists": result,
    }


def _inventory_meta(conn: sqlite3.Connection) -> sqlite3.Row:
    ensure_playlist_sync_schema(conn)
    row = conn.execute("SELECT * FROM youtube_playlist_inventory_meta WHERE singleton = 1").fetchone()
    if row is None:
        raise ValueError(
            "no playlist inventory has been imported; import/refresh destination playlists before planning"
        )
    return row


def _destination_matches(conn: sqlite3.Connection, title: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT playlist_id, title, privacy_status
        FROM youtube_playlist_inventory
        WHERE title = ? COLLATE NOCASE
        ORDER BY playlist_id
        """,
        (title,),
    ).fetchall()


def create_plan(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    backend: str = "api",
    new_playlist_privacy: str = "private",
    quota_limit: int = DEFAULT_API_QUOTA_LIMIT,
    playlist_create_cost: int = DEFAULT_PLAYLIST_CREATE_COST,
    playlist_insert_cost: int = DEFAULT_PLAYLIST_INSERT_COST,
) -> PlaylistPlan:
    ensure_playlist_sync_schema(conn)
    backend = backend.casefold()
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(sorted(BACKENDS))}")
    new_playlist_privacy = _privacy(
        new_playlist_privacy,
        "new playlist privacy",
        allow_unknown=False,
    )
    for name, value in (
        ("quota limit", quota_limit),
        ("playlist create cost", playlist_create_cost),
        ("playlist insert cost", playlist_insert_cost),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    snapshot = conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if snapshot is None:
        raise ValueError(f"snapshot {snapshot_id} does not exist")

    decisions = conn.execute(
        """
        SELECT d.id AS decision_event_id, d.video_id, d.destination_playlist,
               e.position
        FROM current_decisions AS d
        JOIN snapshot_entries AS e
          ON e.snapshot_id = d.snapshot_id AND e.video_id = d.video_id
        WHERE d.snapshot_id = ? AND d.action = 'move'
        ORDER BY e.position, d.video_id
        """,
        (snapshot_id,),
    ).fetchall()
    if not decisions:
        raise ValueError(
            "snapshot has no current move decisions; watchlater-playlist only plans destination "
            "moves. For archive/delete removal without destination playlists, run "
            "'watchlater-remove plan' instead"
        )

    try:
        meta = _inventory_meta(conn)
    except ValueError as exc:
        raise ValueError(
            f"{exc}. For archive/delete removal without destination playlists, run "
            "'watchlater-remove plan' instead"
        ) from exc

    destination_info: dict[str, dict[str, Any]] = {}
    for row in decisions:
        destination = str(row["destination_playlist"] or "")
        if not destination:
            raise ValueError(f"move decision for {row['video_id']} has no destination playlist")
        if destination in destination_info:
            continue
        matches = _destination_matches(conn, destination)
        if len(matches) > 1:
            ids = ", ".join(str(match["playlist_id"]) for match in matches)
            raise ValueError(
                f"destination title {destination!r} is ambiguous in playlist inventory: {ids}"
            )
        if len(matches) == 1:
            match = matches[0]
            destination_info[destination] = {
                "destination_name": destination,
                "playlist_id": str(match["playlist_id"]),
                "privacy_status": str(match["privacy_status"]),
                "status": "existing",
            }
        else:
            destination_info[destination] = {
                "destination_name": destination,
                "playlist_id": None,
                "privacy_status": new_playlist_privacy,
                "status": "create_planned",
            }

    item_rows: list[dict[str, Any]] = []
    insert_count = 0
    already_present = 0
    for ordinal, row in enumerate(decisions, start=1):
        destination = str(row["destination_playlist"])
        dest = destination_info[destination]
        existing_item = None
        if dest["playlist_id"] is not None:
            existing_item = conn.execute(
                """
                SELECT playlist_item_id
                FROM youtube_playlist_inventory_items
                WHERE playlist_id = ? AND video_id = ?
                """,
                (dest["playlist_id"], row["video_id"]),
            ).fetchone()
        if existing_item is not None:
            status = "already_present"
            playlist_item_id = existing_item["playlist_item_id"]
            already_present += 1
        else:
            status = "planned"
            playlist_item_id = None
            insert_count += 1
        item_rows.append(
            {
                "ordinal": ordinal,
                "video_id": str(row["video_id"]),
                "decision_event_id": int(row["decision_event_id"]),
                "position": int(row["position"]),
                "destination_name": destination,
                "destination_playlist_id": dest["playlist_id"],
                "status": status,
                "playlist_item_id": playlist_item_id,
            }
        )

    create_count = sum(1 for row in destination_info.values() if row["status"] == "create_planned")
    estimated_quota = 0
    if backend == "api":
        estimated_quota = create_count * playlist_create_cost + insert_count * playlist_insert_cost
    exceeds = estimated_quota > quota_limit

    plan_payload = {
        "version": 1,
        "snapshot_id": snapshot_id,
        "backend": backend,
        "inventory_fetched_at": meta["fetched_at"],
        "new_playlist_privacy": new_playlist_privacy,
        "quota": {
            "limit": quota_limit,
            "playlist_create_cost": playlist_create_cost,
            "playlist_insert_cost": playlist_insert_cost,
            "estimated": estimated_quota,
            "exceeds": exceeds,
        },
        "destinations": list(destination_info.values()),
        "items": item_rows,
    }

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO playlist_sync_runs (
                snapshot_id, created_at, backend, status, inventory_fetched_at,
                new_playlist_privacy, playlist_create_cost, playlist_insert_cost,
                quota_limit, estimated_quota, exceeds_quota, plan_json
            ) VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                _utc_now(),
                backend,
                meta["fetched_at"],
                new_playlist_privacy,
                playlist_create_cost,
                playlist_insert_cost,
                quota_limit,
                estimated_quota,
                1 if exceeds else 0,
                json.dumps(plan_payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in destination_info.values():
            conn.execute(
                """
                INSERT INTO playlist_sync_destinations (
                    run_id, destination_name, destination_playlist_id,
                    privacy_status, status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["destination_name"],
                    row["playlist_id"],
                    row["privacy_status"],
                    row["status"],
                ),
            )
        for row in item_rows:
            conn.execute(
                """
                INSERT INTO playlist_sync_items (
                    run_id, ordinal, video_id, decision_event_id,
                    destination_name, destination_playlist_id, status, playlist_item_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["ordinal"],
                    row["video_id"],
                    row["decision_event_id"],
                    row["destination_name"],
                    row["destination_playlist_id"],
                    row["status"],
                    row["playlist_item_id"],
                ),
            )

    return PlaylistPlan(
        run_id=run_id,
        snapshot_id=snapshot_id,
        backend=backend,
        destination_count=len(destination_info),
        create_count=create_count,
        insert_count=insert_count,
        already_present_count=already_present,
        estimated_quota=estimated_quota,
        quota_limit=quota_limit,
        exceeds_quota=exceeds,
    )


def plan_payload(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    ensure_playlist_sync_schema(conn)
    run = conn.execute("SELECT * FROM playlist_sync_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"playlist sync plan {run_id} does not exist")
    destinations = conn.execute(
        """
        SELECT destination_name, destination_playlist_id, privacy_status,
               status, created_at, error
        FROM playlist_sync_destinations
        WHERE run_id = ? ORDER BY destination_name COLLATE NOCASE
        """,
        (run_id,),
    ).fetchall()
    items = conn.execute(
        """
        SELECT i.*, d.id AS current_decision_event_id,
               d.action AS current_action,
               d.destination_playlist AS current_destination
        FROM playlist_sync_items AS i
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = ? AND d.video_id = i.video_id
        WHERE i.run_id = ?
        ORDER BY i.ordinal
        """,
        (run["snapshot_id"], run_id),
    ).fetchall()
    item_payload = []
    stale_count = 0
    for row in items:
        stale = not (
            row["current_decision_event_id"] == row["decision_event_id"]
            and row["current_action"] == "move"
            and row["current_destination"] == row["destination_name"]
        )
        stale_count += int(stale)
        item_payload.append(
            {
                "ordinal": int(row["ordinal"]),
                "video_id": row["video_id"],
                "decision_event_id": int(row["decision_event_id"]),
                "destination_name": row["destination_name"],
                "destination_playlist_id": row["destination_playlist_id"],
                "status": row["status"],
                "playlist_item_id": row["playlist_item_id"],
                "attempted_at": row["attempted_at"],
                "completed_at": row["completed_at"],
                "error": row["error"],
                "stale": stale,
            }
        )
    return {
        "run_id": run_id,
        "snapshot_id": int(run["snapshot_id"]),
        "created_at": run["created_at"],
        "backend": run["backend"],
        "status": run["status"],
        "inventory_fetched_at": run["inventory_fetched_at"],
        "new_playlist_privacy": run["new_playlist_privacy"],
        "quota": {
            "limit": int(run["quota_limit"]),
            "playlist_create_cost": int(run["playlist_create_cost"]),
            "playlist_insert_cost": int(run["playlist_insert_cost"]),
            "estimated": int(run["estimated_quota"]),
            "exceeds": bool(run["exceeds_quota"]),
        },
        "stale_item_count": stale_count,
        "destinations": [dict(row) for row in destinations],
        "items": item_payload,
    }


def latest_plan_id(conn: sqlite3.Connection, snapshot_id: int | None = None) -> int:
    ensure_playlist_sync_schema(conn)
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    row = conn.execute(
        "SELECT id FROM playlist_sync_runs WHERE snapshot_id = ? ORDER BY id DESC LIMIT 1",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"snapshot {snapshot_id} has no playlist sync plan")
    return int(row["id"])
