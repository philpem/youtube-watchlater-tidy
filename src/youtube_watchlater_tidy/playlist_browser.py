from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, TypeVar

from .playlist_sync import ensure_playlist_sync_schema, plan_payload


@dataclass(frozen=True)
class BrowserPlaylist:
    playlist_id: str
    title: str
    privacy_status: str = "unknown"


@dataclass(frozen=True)
class BrowserPlaylistItem:
    playlist_item_id: str | None = None


class PlaylistBrowserClient(Protocol):
    def list_playlists(self) -> list[BrowserPlaylist]: ...

    def find_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem | None: ...

    def create_playlist(self, title: str, privacy_status: str) -> BrowserPlaylist: ...

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem: ...


@dataclass(frozen=True)
class BrowserExecutionResult:
    run_id: int
    applied: bool
    created_playlists: int
    inserted: int
    already_present: int
    failed: int
    stale: int
    remaining: int
    writes: int
    run_status: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_decision_matches(conn: sqlite3.Connection, run: sqlite3.Row, item: sqlite3.Row) -> bool:
    row = conn.execute(
        """
        SELECT id, action, destination_playlist
        FROM current_decisions
        WHERE snapshot_id = ? AND video_id = ?
        """,
        (run["snapshot_id"], item["video_id"]),
    ).fetchone()
    return bool(
        row is not None
        and row["id"] == item["decision_event_id"]
        and row["action"] == "move"
        and row["destination_playlist"] == item["destination_name"]
    )


def _checkpoint_destination(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    name: str,
    playlist: BrowserPlaylist,
    status: str,
) -> None:
    now = _utc_now()
    raw = {
        "playlist_id": playlist.playlist_id,
        "title": playlist.title,
        "privacy_status": playlist.privacy_status,
    }
    with conn:
        conn.execute(
            """
            UPDATE playlist_sync_destinations
            SET destination_playlist_id = ?, status = ?, privacy_status = ?,
                created_at = CASE WHEN ? = 'created' THEN ? ELSE created_at END,
                error = NULL
            WHERE run_id = ? AND destination_name = ?
            """,
            (
                playlist.playlist_id,
                status,
                playlist.privacy_status,
                status,
                now,
                run_id,
                name,
            ),
        )
        conn.execute(
            """
            INSERT INTO youtube_playlist_inventory (
                playlist_id, title, privacy_status, fetched_at, source, raw_json
            ) VALUES (?, ?, ?, ?, 'browser-live', ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
                title = excluded.title,
                privacy_status = excluded.privacy_status,
                fetched_at = excluded.fetched_at,
                source = excluded.source,
                raw_json = excluded.raw_json
            """,
            (
                playlist.playlist_id,
                playlist.title,
                playlist.privacy_status,
                now,
                json.dumps(raw, sort_keys=True),
            ),
        )


def _checkpoint_item(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    ordinal: int,
    video_id: str,
    playlist_id: str | None,
    status: str,
    playlist_item_id: str | None = None,
    error: str | None = None,
) -> None:
    now = _utc_now()
    with conn:
        conn.execute(
            """
            UPDATE playlist_sync_items
            SET status = ?, destination_playlist_id = ?, playlist_item_id = ?,
                attempted_at = ?,
                completed_at = CASE WHEN ? IN ('inserted', 'already_present') THEN ? ELSE completed_at END,
                error = ?
            WHERE run_id = ? AND ordinal = ?
            """,
            (status, playlist_id, playlist_item_id, now, status, now, error, run_id, ordinal),
        )
        if status in {"inserted", "already_present"} and playlist_id:
            conn.execute(
                """
                INSERT INTO youtube_playlist_inventory_items (
                    playlist_id, video_id, playlist_item_id, position, raw_json
                ) VALUES (?, ?, ?, NULL, '{}')
                ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                    playlist_item_id = excluded.playlist_item_id,
                    raw_json = excluded.raw_json
                """,
                (playlist_id, video_id, playlist_item_id),
            )


def _finish_run(conn: sqlite3.Connection, run_id: int) -> tuple[str, int]:
    remaining = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM playlist_sync_items
            WHERE run_id = ?
              AND (
                    status IN ('planned', 'failed', 'skipped')
                    OR (status = 'already_present' AND attempted_at IS NULL)
              )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    status = "complete" if remaining == 0 else "partial"
    with conn:
        conn.execute("UPDATE playlist_sync_runs SET status = ? WHERE id = ?", (status, run_id))
    return status, remaining


T = TypeVar("T")


def _retry(
    fn: Callable[[], T],
    *,
    retries: int,
    backoff: float,
    sleeper: Callable[[float], None],
) -> T:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # browser/UI failures are retriable at this boundary
            last = exc
            if attempt >= retries:
                raise
            if backoff:
                sleeper(backoff * (2**attempt))
    assert last is not None
    raise last


def execute_browser_plan(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    client: PlaylistBrowserClient | None = None,
    apply: bool = False,
    max_writes: int | None = None,
    interval: float = 2.0,
    retries: int = 1,
    backoff: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> BrowserExecutionResult:
    """Inspect or execute one persisted browser-backend playlist plan.

    This module deliberately contains no Playwright selectors. A later UI adapter implements
    PlaylistBrowserClient; tests can supply a fake client now. With apply=False there is no
    browser/network access and no checkpoint mutation.
    """

    ensure_playlist_sync_schema(conn)
    if max_writes is not None and max_writes < 0:
        raise ValueError("--max-writes cannot be negative")
    if interval < 0:
        raise ValueError("--interval cannot be negative")
    if retries < 0:
        raise ValueError("--retries cannot be negative")
    if backoff < 0:
        raise ValueError("--backoff cannot be negative")

    run = conn.execute("SELECT * FROM playlist_sync_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"playlist sync plan {run_id} does not exist")
    if run["backend"] != "browser":
        raise ValueError(f"playlist sync plan {run_id} uses backend {run['backend']!r}, not 'browser'")

    payload = plan_payload(conn, run_id)
    stale = int(payload["stale_item_count"])
    if apply and stale:
        raise ValueError(
            f"playlist sync plan {run_id} contains {stale} stale item(s); create a fresh plan before applying"
        )

    counts = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM playlist_sync_items WHERE run_id = ? GROUP BY status",
            (run_id,),
        )
    }
    if not apply:
        remaining = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM playlist_sync_items
                WHERE run_id = ? AND (
                    status IN ('planned', 'failed', 'skipped')
                    OR (status='already_present' AND attempted_at IS NULL)
                )
                """,
                (run_id,),
            ).fetchone()[0]
        )
        return BrowserExecutionResult(
            run_id=run_id,
            applied=False,
            created_playlists=0,
            inserted=0,
            already_present=counts.get("already_present", 0),
            failed=counts.get("failed", 0),
            stale=stale,
            remaining=remaining,
            writes=0,
            run_status=str(run["status"]),
        )

    if client is None:
        raise ValueError("a browser playlist client is required with --apply")

    live = _retry(client.list_playlists, retries=retries, backoff=backoff, sleeper=sleeper)
    by_id = {row.playlist_id: row for row in live}
    by_title: dict[str, list[BrowserPlaylist]] = {}
    for row in live:
        by_title.setdefault(row.title.casefold(), []).append(row)

    with conn:
        conn.execute("UPDATE playlist_sync_runs SET status='running' WHERE id=?", (run_id,))

    writes = created = inserted = present = failed = stale_runtime = 0
    stopped_for_cap = False

    def can_write() -> bool:
        return max_writes is None or writes < max_writes

    items = conn.execute(
        """
        SELECT * FROM playlist_sync_items
        WHERE run_id = ? AND (
            status IN ('planned', 'failed', 'skipped')
            OR (status='already_present' AND attempted_at IS NULL)
        )
        ORDER BY ordinal
        """,
        (run_id,),
    ).fetchall()

    for item in items:
        if not _current_decision_matches(conn, run, item):
            stale_runtime += 1
            continue

        destination = conn.execute(
            "SELECT * FROM playlist_sync_destinations WHERE run_id=? AND destination_name=?",
            (run_id, item["destination_name"]),
        ).fetchone()
        assert destination is not None
        playlist_id = destination["destination_playlist_id"]

        try:
            if playlist_id:
                playlist_id = str(playlist_id)
                if playlist_id not in by_id:
                    raise RuntimeError(
                        f"planned destination {item['destination_name']!r} had playlist id {playlist_id!r}, "
                        "but that id is not present in the live browser account; refresh inventory and re-plan"
                    )
            else:
                matches = by_title.get(str(item["destination_name"]).casefold(), [])
                if len(matches) > 1:
                    raise RuntimeError(
                        f"destination title {item['destination_name']!r} is ambiguous in the live browser account"
                    )
                if len(matches) == 1:
                    playlist = matches[0]
                    playlist_id = playlist.playlist_id
                    _checkpoint_destination(
                        conn,
                        run_id=run_id,
                        name=str(item["destination_name"]),
                        playlist=playlist,
                        status="existing",
                    )
                else:
                    if not can_write():
                        stopped_for_cap = True
                        break
                    playlist = _retry(
                        lambda: client.create_playlist(
                            str(item["destination_name"]), str(destination["privacy_status"])
                        ),
                        retries=retries,
                        backoff=backoff,
                        sleeper=sleeper,
                    )
                    writes += 1
                    created += 1
                    playlist_id = playlist.playlist_id
                    by_id[playlist_id] = playlist
                    by_title.setdefault(playlist.title.casefold(), []).append(playlist)
                    _checkpoint_destination(
                        conn,
                        run_id=run_id,
                        name=str(item["destination_name"]),
                        playlist=playlist,
                        status="created",
                    )
                    if interval:
                        sleeper(interval)

            if not _current_decision_matches(conn, run, item):
                stale_runtime += 1
                continue

            existing = _retry(
                lambda: client.find_playlist_item(str(playlist_id), str(item["video_id"])),
                retries=retries,
                backoff=backoff,
                sleeper=sleeper,
            )
            if existing is not None:
                present += 1
                _checkpoint_item(
                    conn,
                    run_id=run_id,
                    ordinal=int(item["ordinal"]),
                    video_id=str(item["video_id"]),
                    playlist_id=str(playlist_id),
                    playlist_item_id=existing.playlist_item_id,
                    status="already_present",
                )
                continue

            if not can_write():
                stopped_for_cap = True
                break
            added = _retry(
                lambda: client.insert_playlist_item(str(playlist_id), str(item["video_id"])),
                retries=retries,
                backoff=backoff,
                sleeper=sleeper,
            )
            writes += 1
            inserted += 1
            _checkpoint_item(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                video_id=str(item["video_id"]),
                playlist_id=str(playlist_id),
                playlist_item_id=added.playlist_item_id,
                status="inserted",
            )
            if interval:
                sleeper(interval)
        except Exception as exc:
            failed += 1
            _checkpoint_item(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                video_id=str(item["video_id"]),
                playlist_id=str(playlist_id) if playlist_id else None,
                status="failed",
                error=str(exc),
            )

    status, remaining = _finish_run(conn, run_id)
    if stopped_for_cap and status == "complete":
        status = "partial"
        with conn:
            conn.execute("UPDATE playlist_sync_runs SET status='partial' WHERE id=?", (run_id,))

    return BrowserExecutionResult(
        run_id=run_id,
        applied=True,
        created_playlists=created,
        inserted=inserted,
        already_present=present,
        failed=failed,
        stale=stale_runtime,
        remaining=remaining,
        writes=writes,
        run_status=status,
    )
