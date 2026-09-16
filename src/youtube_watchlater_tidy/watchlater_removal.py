from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .playlist_sync import ensure_playlist_sync_schema
from .reports import latest_snapshot_id

REMOVAL_ACTIONS = {"delete", "archive", "move"}
TERMINAL_REMOVAL_STATUSES = {"removed", "already_absent"}
RETRIABLE_REMOVAL_STATUSES = {"planned", "not_found", "failed", "skipped"}

WATCHLATER_REMOVAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchlater_removal_runs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned', 'running', 'partial', 'complete', 'cancelled')),
    plan_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watchlater_removal_runs_snapshot
    ON watchlater_removal_runs(snapshot_id, id);

CREATE TABLE IF NOT EXISTS watchlater_removal_items (
    run_id INTEGER NOT NULL REFERENCES watchlater_removal_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    decision_event_id INTEGER NOT NULL REFERENCES decision_events(id),
    action TEXT NOT NULL CHECK(action IN ('delete', 'archive', 'move')),
    destination_playlist TEXT,
    destination_sync_run_id INTEGER REFERENCES playlist_sync_runs(id),
    status TEXT NOT NULL CHECK(status IN ('planned', 'removed', 'already_absent', 'not_found', 'failed', 'skipped')),
    attempted_at TEXT,
    completed_at TEXT,
    error TEXT,
    PRIMARY KEY (run_id, ordinal),
    UNIQUE (run_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_watchlater_removal_items_status
    ON watchlater_removal_items(run_id, status, ordinal);
"""


@dataclass(frozen=True)
class RemovalPlan:
    run_id: int
    snapshot_id: int
    eligible_count: int
    delete_count: int
    archive_count: int
    move_count: int
    blocked_move_count: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_watchlater_removal_schema(conn: sqlite3.Connection) -> None:
    # The removal table has an optional FK into playlist_sync_runs for confirmed moves.
    # Initialize that referenced schema even for delete/archive-only catalogues.
    ensure_playlist_sync_schema(conn)
    with conn:
        conn.executescript(WATCHLATER_REMOVAL_SCHEMA_SQL)


def _confirmed_move_checkpoint(
    conn: sqlite3.Connection,
    *,
    decision_event_id: int,
    video_id: str,
    destination_playlist: str,
) -> int | None:
    """Return a sync run confirming the exact move decision, or None.

    An inventory-only `already_present` row is not sufficient. API/browser execution must
    have revalidated it live, represented by attempted_at being non-null.
    """

    row = conn.execute(
        """
        SELECT i.run_id
        FROM playlist_sync_items AS i
        JOIN playlist_sync_runs AS r ON r.id = i.run_id
        WHERE i.decision_event_id = ?
          AND i.video_id = ?
          AND i.destination_name = ?
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
    ensure_watchlater_removal_schema(conn)
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
        destination = row["destination_playlist"]
        sync_run_id = None
        if action == "move":
            if not destination:
                blocked_moves.append(
                    {
                        "video_id": str(row["video_id"]),
                        "decision_event_id": int(row["decision_event_id"]),
                        "reason": "move decision has no destination playlist",
                    }
                )
                continue
            sync_run_id = _confirmed_move_checkpoint(
                conn,
                decision_event_id=int(row["decision_event_id"]),
                video_id=str(row["video_id"]),
                destination_playlist=str(destination),
            )
            if sync_run_id is None:
                blocked_moves.append(
                    {
                        "video_id": str(row["video_id"]),
                        "decision_event_id": int(row["decision_event_id"]),
                        "destination_playlist": str(destination),
                        "reason": "destination insertion is not confirmed for this exact move decision",
                    }
                )
                continue
        counts[action] += 1
        eligible.append(
            {
                "ordinal": len(eligible) + 1,
                "position": int(row["position"]),
                "video_id": str(row["video_id"]),
                "decision_event_id": int(row["decision_event_id"]),
                "action": action,
                "destination_playlist": destination,
                "destination_sync_run_id": sync_run_id,
            }
        )

    payload = {
        "version": 1,
        "snapshot_id": snapshot_id,
        "eligible": eligible,
        "blocked_moves": blocked_moves,
    }
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


def _current_item_is_authorized(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    item: sqlite3.Row,
) -> bool:
    row = conn.execute(
        """
        SELECT id, action, destination_playlist
        FROM current_decisions
        WHERE snapshot_id = ? AND video_id = ?
        """,
        (snapshot_id, item["video_id"]),
    ).fetchone()
    if row is None:
        return False
    if int(row["id"]) != int(item["decision_event_id"]):
        return False
    if row["action"] != item["action"]:
        return False
    if item["action"] == "move":
        if row["destination_playlist"] != item["destination_playlist"]:
            return False
        confirmed = _confirmed_move_checkpoint(
            conn,
            decision_event_id=int(item["decision_event_id"]),
            video_id=str(item["video_id"]),
            destination_playlist=str(item["destination_playlist"]),
        )
        return confirmed is not None
    return True


def removal_plan_payload(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    ensure_watchlater_removal_schema(conn)
    run = conn.execute("SELECT * FROM watchlater_removal_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Watch Later removal plan {run_id} does not exist")
    rows = conn.execute(
        "SELECT * FROM watchlater_removal_items WHERE run_id = ? ORDER BY ordinal",
        (run_id,),
    ).fetchall()
    items = []
    stale_count = 0
    for row in rows:
        stale = not _current_item_is_authorized(
            conn,
            snapshot_id=int(run["snapshot_id"]),
            item=row,
        )
        stale_count += int(stale)
        items.append(
            {
                "ordinal": int(row["ordinal"]),
                "video_id": row["video_id"],
                "decision_event_id": int(row["decision_event_id"]),
                "action": row["action"],
                "destination_playlist": row["destination_playlist"],
                "destination_sync_run_id": row["destination_sync_run_id"],
                "status": row["status"],
                "attempted_at": row["attempted_at"],
                "completed_at": row["completed_at"],
                "error": row["error"],
                "stale": stale,
            }
        )
    original = json.loads(run["plan_json"])
    return {
        "run_id": run_id,
        "snapshot_id": int(run["snapshot_id"]),
        "created_at": run["created_at"],
        "status": run["status"],
        "stale_item_count": stale_count,
        "blocked_moves": original.get("blocked_moves", []),
        "items": items,
    }


def latest_removal_plan_id(conn: sqlite3.Connection, snapshot_id: int | None = None) -> int:
    ensure_watchlater_removal_schema(conn)
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    row = conn.execute(
        "SELECT id FROM watchlater_removal_runs WHERE snapshot_id = ? ORDER BY id DESC LIMIT 1",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"snapshot {snapshot_id} has no Watch Later removal plan")
    return int(row["id"])


def checkpoint_removal(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    ordinal: int,
    status: str,
    error: str | None = None,
) -> None:
    if status not in {"removed", "already_absent", "not_found", "failed", "skipped"}:
        raise ValueError(f"invalid removal status {status!r}")
    now = _utc_now()
    terminal = status in TERMINAL_REMOVAL_STATUSES
    with conn:
        cursor = conn.execute(
            """
            UPDATE watchlater_removal_items
            SET status = ?, attempted_at = ?,
                completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                error = ?
            WHERE run_id = ? AND ordinal = ?
            """,
            (status, now, 1 if terminal else 0, now, error, run_id, ordinal),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Watch Later removal item {run_id}:{ordinal} does not exist")


def finish_removal_run(conn: sqlite3.Connection, run_id: int) -> tuple[str, int]:
    remaining = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM watchlater_removal_items
            WHERE run_id = ? AND status NOT IN ('removed', 'already_absent')
            """,
            (run_id,),
        ).fetchone()[0]
    )
    status = "complete" if remaining == 0 else "partial"
    with conn:
        conn.execute("UPDATE watchlater_removal_runs SET status = ? WHERE id = ?", (status, run_id))
    return status, remaining


def pending_removal_items(conn: sqlite3.Connection, run_id: int) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    ensure_watchlater_removal_schema(conn)
    run = conn.execute("SELECT * FROM watchlater_removal_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Watch Later removal plan {run_id} does not exist")
    items = conn.execute(
        """
        SELECT * FROM watchlater_removal_items
        WHERE run_id = ? AND status IN ('planned', 'not_found', 'failed', 'skipped')
        ORDER BY ordinal
        """,
        (run_id,),
    ).fetchall()
    return run, list(items)


def removal_item_is_authorized(conn: sqlite3.Connection, run: sqlite3.Row, item: sqlite3.Row) -> bool:
    return _current_item_is_authorized(
        conn,
        snapshot_id=int(run["snapshot_id"]),
        item=item,
    )
