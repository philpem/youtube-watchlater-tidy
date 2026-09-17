from __future__ import annotations

import json
import sqlite3
from typing import Any

from .multi_destination import (
    decision_has_destination,
    destinations_for_decision,
    ensure_decision_destinations_schema,
)
from .playlist_sync import ensure_playlist_sync_schema
from .reports import latest_snapshot_id
from .watchlater_removal import RemovalPlan, ensure_watchlater_removal_schema


def plan_payload(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Render one playlist plan with multi-destination-aware staleness checks."""

    ensure_playlist_sync_schema(conn)
    ensure_decision_destinations_schema(conn)
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
               d.action AS current_action
        FROM playlist_sync_items AS i
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = ? AND d.video_id = i.video_id
        WHERE i.run_id = ?
        ORDER BY i.ordinal
        """,
        (run["snapshot_id"], run_id),
    ).fetchall()
    item_payload: list[dict[str, Any]] = []
    stale_count = 0
    for row in items:
        event_id = row["current_decision_event_id"]
        stale = not (
            event_id == row["decision_event_id"]
            and row["current_action"] == "move"
            and event_id is not None
            and decision_has_destination(
                conn,
                int(event_id),
                str(row["destination_name"]),
            )
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


def _confirmed_destination_run(
    conn: sqlite3.Connection,
    *,
    decision_event_id: int,
    video_id: str,
    destination_playlist: str,
) -> int | None:
    ensure_playlist_sync_schema(conn)
    row = conn.execute(
        """
        SELECT i.run_id
        FROM playlist_sync_items AS i
        WHERE i.decision_event_id = ?
          AND i.video_id = ?
          AND i.destination_name = ? COLLATE NOCASE
          AND (
                i.status = 'inserted'
                OR (i.status = 'already_present' AND i.attempted_at IS NOT NULL)
          )
        ORDER BY i.run_id DESC
        LIMIT 1
        """,
        (decision_event_id, video_id, destination_playlist),
    ).fetchone()
    return None if row is None else int(row["run_id"])


def create_removal_plan(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
) -> RemovalPlan:
    """Create a Watch Later removal plan requiring every move destination to be confirmed."""

    ensure_watchlater_removal_schema(conn)
    ensure_playlist_sync_schema(conn)
    ensure_decision_destinations_schema(conn)
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone() is None:
        raise ValueError(f"snapshot {snapshot_id} does not exist")

    rows = conn.execute(
        """
        SELECT d.id AS decision_event_id, d.video_id, d.action,
               d.destination_playlist, e.position
        FROM current_decisions AS d
        JOIN snapshot_entries AS e
          ON e.snapshot_id = d.snapshot_id AND e.video_id = d.video_id
        WHERE d.snapshot_id = ?
          AND d.action IN ('delete', 'archive', 'move')
        ORDER BY e.position, d.video_id
        """,
        (snapshot_id,),
    ).fetchall()

    eligible: list[dict[str, Any]] = []
    blocked_moves: list[dict[str, Any]] = []
    counts = {"delete": 0, "archive": 0, "move": 0}
    for row in rows:
        action = str(row["action"])
        primary = row["destination_playlist"]
        sync_run_id = None
        if action == "move":
            destinations = destinations_for_decision(conn, int(row["decision_event_id"]))
            if not destinations:
                blocked_moves.append(
                    {
                        "video_id": str(row["video_id"]),
                        "decision_event_id": int(row["decision_event_id"]),
                        "reason": "move decision has no destination playlists",
                    }
                )
                continue
            missing: list[str] = []
            confirmed_runs: list[int] = []
            for destination in destinations:
                confirmed = _confirmed_destination_run(
                    conn,
                    decision_event_id=int(row["decision_event_id"]),
                    video_id=str(row["video_id"]),
                    destination_playlist=destination,
                )
                if confirmed is None:
                    missing.append(destination)
                else:
                    confirmed_runs.append(confirmed)
            if missing:
                blocked_moves.append(
                    {
                        "video_id": str(row["video_id"]),
                        "decision_event_id": int(row["decision_event_id"]),
                        "destination_playlists": list(destinations),
                        "missing_destinations": missing,
                        "reason": "not every destination is confirmed for this exact move decision",
                    }
                )
                continue
            primary = destinations[0]
            sync_run_id = max(confirmed_runs) if confirmed_runs else None

        counts[action] += 1
        eligible.append(
            {
                "ordinal": len(eligible) + 1,
                "position": int(row["position"]),
                "video_id": str(row["video_id"]),
                "decision_event_id": int(row["decision_event_id"]),
                "action": action,
                "destination_playlist": primary,
                "destination_sync_run_id": sync_run_id,
            }
        )

    payload = {
        "version": 2,
        "snapshot_id": snapshot_id,
        "eligible": eligible,
        "blocked_moves": blocked_moves,
    }
    from .watchlater_removal import _utc_now  # keep timestamp semantics aligned

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO watchlater_removal_runs (
                snapshot_id, created_at, status, plan_json
            ) VALUES (?, ?, 'planned', ?)
            """,
            (snapshot_id, _utc_now(), json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        run_id = int(cursor.lastrowid)
        for item in eligible:
            conn.execute(
                """
                INSERT INTO watchlater_removal_items (
                    run_id, ordinal, video_id, decision_event_id, action,
                    destination_playlist, destination_sync_run_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned')
                """,
                (
                    run_id,
                    item["ordinal"],
                    item["video_id"],
                    item["decision_event_id"],
                    item["action"],
                    item["destination_playlist"],
                    item["destination_sync_run_id"],
                ),
            )

    return RemovalPlan(
        run_id=run_id,
        snapshot_id=snapshot_id,
        eligible_count=len(eligible),
        delete_count=counts["delete"],
        archive_count=counts["archive"],
        move_count=counts["move"],
        blocked_move_count=len(blocked_moves),
    )
