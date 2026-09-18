from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from .playlist_sync import (
    DEFAULT_API_QUOTA_LIMIT,
    DEFAULT_PLAYLIST_CREATE_COST,
    DEFAULT_PLAYLIST_INSERT_COST,
    BACKENDS,
    PlaylistPlan,
    ensure_playlist_sync_schema,
)
from .reports import latest_snapshot_id
from .triage import latest_selection_id

DECISION_DESTINATIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decision_event_destinations (
    decision_event_id INTEGER NOT NULL REFERENCES decision_events(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    destination_playlist TEXT NOT NULL,
    PRIMARY KEY (decision_event_id, destination_playlist),
    UNIQUE (decision_event_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_decision_event_destinations_event
    ON decision_event_destinations(decision_event_id, ordinal);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_decision_destinations_schema(conn: sqlite3.Connection) -> None:
    """Create/backfill the destination child table without rewriting decision_events."""

    with conn:
        conn.executescript(DECISION_DESTINATIONS_SCHEMA_SQL)
        conn.execute(
            """
            INSERT OR IGNORE INTO decision_event_destinations (
                decision_event_id, ordinal, destination_playlist
            )
            SELECT id, 1, destination_playlist
            FROM decision_events
            WHERE action = 'move' AND destination_playlist IS NOT NULL
            """
        )


def normalize_destinations(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        destination = str(value).strip()
        if not destination:
            raise ValueError("destination playlist names must not be empty")
        folded = destination.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(destination)
    if not result:
        raise ValueError("at least one --playlist is required")
    return tuple(result)


def destinations_for_decision(conn: sqlite3.Connection, decision_event_id: int) -> tuple[str, ...]:
    ensure_decision_destinations_schema(conn)
    rows = conn.execute(
        """
        SELECT destination_playlist
        FROM decision_event_destinations
        WHERE decision_event_id = ?
        ORDER BY ordinal
        """,
        (decision_event_id,),
    ).fetchall()
    return tuple(str(row["destination_playlist"]) for row in rows)


def decision_has_destination(
    conn: sqlite3.Connection,
    decision_event_id: int,
    destination_playlist: str,
) -> bool:
    ensure_decision_destinations_schema(conn)
    row = conn.execute(
        """
        SELECT 1
        FROM decision_event_destinations
        WHERE decision_event_id = ? AND destination_playlist = ? COLLATE NOCASE
        """,
        (decision_event_id, destination_playlist),
    ).fetchone()
    return row is not None


def current_move_authorizes_destination(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    video_id: str,
    decision_event_id: int,
    destination_playlist: str,
) -> bool:
    ensure_decision_destinations_schema(conn)
    row = conn.execute(
        """
        SELECT id, action
        FROM current_decisions
        WHERE snapshot_id = ? AND video_id = ?
        """,
        (snapshot_id, video_id),
    ).fetchone()
    return bool(
        row is not None
        and int(row["id"]) == decision_event_id
        and row["action"] == "move"
        and decision_has_destination(conn, decision_event_id, destination_playlist)
    )


def record_selection_move(
    conn: sqlite3.Connection,
    *,
    destinations: Iterable[str],
    selection_id: int | None = None,
    snapshot_id: int | None = None,
    reason: str | None = None,
    source: str = "human",
) -> int:
    """Record one move decision per selected video with an ordered destination set."""

    ensure_decision_destinations_schema(conn)
    normalized = normalize_destinations(destinations)
    if selection_id is None:
        selection_id = latest_selection_id(conn, snapshot_id)
    selection = conn.execute(
        "SELECT snapshot_id FROM selections WHERE id = ?", (selection_id,)
    ).fetchone()
    if selection is None:
        raise ValueError(f"selection {selection_id} does not exist")
    selection_snapshot = int(selection["snapshot_id"])
    if snapshot_id is not None and snapshot_id != selection_snapshot:
        raise ValueError("selection belongs to a different snapshot")

    videos = conn.execute(
        "SELECT video_id FROM selection_entries WHERE selection_id = ? ORDER BY video_id",
        (selection_id,),
    ).fetchall()
    now = _utc_now()
    with conn:
        for row in videos:
            previous = conn.execute(
                "SELECT id FROM current_decisions WHERE snapshot_id = ? AND video_id = ?",
                (selection_snapshot, row["video_id"]),
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO decision_events (
                    snapshot_id, video_id, action, destination_playlist,
                    source, rule_json, reason, created_at, supersedes_id
                ) VALUES (?, ?, 'move', ?, ?, NULL, ?, ?, ?)
                """,
                (
                    selection_snapshot,
                    row["video_id"],
                    normalized[0],
                    source,
                    reason,
                    now,
                    previous["id"] if previous else None,
                ),
            )
            event_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO decision_event_destinations (
                    decision_event_id, ordinal, destination_playlist
                ) VALUES (?, ?, ?)
                """,
                ((event_id, ordinal, destination) for ordinal, destination in enumerate(normalized, 1)),
            )
    return len(videos)


def _privacy(value: str) -> str:
    result = str(value).casefold()
    if result not in {"private", "unlisted", "public"}:
        raise ValueError("new playlist privacy must be private, unlisted or public")
    return result


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
    """Create a playlist-sync plan, expanding every current move destination."""

    ensure_playlist_sync_schema(conn)
    ensure_decision_destinations_schema(conn)
    backend = backend.casefold()
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(sorted(BACKENDS))}")
    new_playlist_privacy = _privacy(new_playlist_privacy)
    for name, value in (
        ("quota limit", quota_limit),
        ("playlist create cost", playlist_create_cost),
        ("playlist insert cost", playlist_insert_cost),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone() is None:
        raise ValueError(f"snapshot {snapshot_id} does not exist")

    decisions = conn.execute(
        """
        SELECT d.id AS decision_event_id, d.video_id, e.position,
               dd.ordinal AS destination_ordinal, dd.destination_playlist
        FROM current_decisions AS d
        JOIN snapshot_entries AS e
          ON e.snapshot_id = d.snapshot_id AND e.video_id = d.video_id
        JOIN decision_event_destinations AS dd
          ON dd.decision_event_id = d.id
        WHERE d.snapshot_id = ? AND d.action = 'move'
        ORDER BY e.position, d.video_id, dd.ordinal
        """,
        (snapshot_id,),
    ).fetchall()
    if not decisions:
        raise ValueError(
            "snapshot has no current move decisions; watchlater-playlist only plans destination "
            "moves. For archive/delete removal without destination playlists, run "
            "'watchlater-remove plan' instead"
        )

    meta = conn.execute(
        "SELECT * FROM youtube_playlist_inventory_meta WHERE singleton = 1"
    ).fetchone()
    if meta is None:
        raise ValueError(
            "no playlist inventory has been imported; import/refresh destination playlists before "
            "planning moves. For archive/delete removal without destination playlists, run "
            "'watchlater-remove plan' instead"
        )

    destination_info: dict[str, dict[str, object]] = {}
    for row in decisions:
        destination = str(row["destination_playlist"])
        if destination in destination_info:
            continue
        matches = conn.execute(
            """
            SELECT playlist_id, title, privacy_status
            FROM youtube_playlist_inventory
            WHERE title = ? COLLATE NOCASE
            ORDER BY playlist_id
            """,
            (destination,),
        ).fetchall()
        if len(matches) > 1:
            ids = ", ".join(str(match["playlist_id"]) for match in matches)
            raise ValueError(
                f"destination title {destination!r} is ambiguous in playlist inventory: {ids}"
            )
        if matches:
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

    item_rows: list[dict[str, object]] = []
    insert_count = already_present = 0
    for ordinal, row in enumerate(decisions, 1):
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
        if existing_item is None:
            status = "planned"
            playlist_item_id = None
            insert_count += 1
        else:
            status = "already_present"
            playlist_item_id = existing_item["playlist_item_id"]
            already_present += 1
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
    estimated_quota = (
        create_count * playlist_create_cost + insert_count * playlist_insert_cost
        if backend == "api"
        else 0
    )
    exceeds = estimated_quota > quota_limit
    payload = {
        "version": 2,
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
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
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
