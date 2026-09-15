from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .recovery import BACKEND_NAME, UNAVAILABLE_TITLES
from .reports import latest_snapshot_id


def recovery_candidate_video_ids(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    unavailable: bool = False,
    video_ids: Iterable[str] | None = None,
    selection_id: int | None = None,
    min_position: int | None = None,
    max_position: int | None = None,
    limit: int | None = None,
    refresh: bool = False,
) -> list[str]:
    """Resolve an explicit archive-recovery target before cache filtering.

    Normal incremental unavailable recovery skips cached found/not-found results.
    ``refresh=True`` disables only that cache filter; it does not change the
    selected cohort.
    """
    explicit_ids = list(dict.fromkeys(video_ids or ()))
    target_count = int(unavailable) + int(bool(explicit_ids)) + int(selection_id is not None)
    if target_count != 1:
        raise ValueError("choose exactly one recovery target: --unavailable, --video-id, or --selection")

    if min_position is not None and max_position is not None and min_position > max_position:
        raise ValueError("--min-position cannot be greater than --max-position")
    if (min_position is not None or max_position is not None) and not unavailable:
        raise ValueError("playlist-position bounds are only valid with --unavailable")

    if selection_id is not None:
        selection = conn.execute(
            "SELECT snapshot_id FROM selections WHERE id = ?",
            (selection_id,),
        ).fetchone()
        if selection is None:
            raise ValueError(f"selection {selection_id} does not exist")
        selection_snapshot = int(selection["snapshot_id"])
        if snapshot_id is not None and snapshot_id != selection_snapshot:
            raise ValueError(
                f"selection {selection_id} belongs to snapshot {selection_snapshot}, not {snapshot_id}"
            )
        snapshot_id = selection_snapshot
    elif snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    assert snapshot_id is not None
    rows = conn.execute(
        """
        SELECT position, video_id, title
        FROM snapshot_entries
        WHERE snapshot_id = ?
        ORDER BY position
        """,
        (snapshot_id,),
    ).fetchall()
    present = {str(row["video_id"]) for row in rows}

    if explicit_ids:
        missing = [video_id for video_id in explicit_ids if video_id not in present]
        if missing:
            raise ValueError(
                f"video id(s) not present in snapshot {snapshot_id}: {', '.join(missing)}"
            )
        selected_ids = set(explicit_ids)
    elif selection_id is not None:
        selected_ids = {
            str(row["video_id"])
            for row in conn.execute(
                "SELECT video_id FROM selection_entries WHERE selection_id = ?",
                (selection_id,),
            )
        }
    else:
        selected_ids = set()

    cached: set[str] = set()
    if not refresh:
        cached = {
            str(row["video_id"])
            for row in conn.execute(
                """
                SELECT video_id
                FROM archive_lookups
                WHERE backend = ? AND status IN ('found', 'not_found')
                GROUP BY video_id
                """,
                (BACKEND_NAME,),
            )
        }

    result: list[str] = []
    for row in rows:
        position = int(row["position"])
        video_id = str(row["video_id"])
        title = (row["title"] or "").casefold()

        if explicit_ids:
            if video_id not in selected_ids:
                continue
        elif selection_id is not None:
            if video_id not in selected_ids:
                continue
            # Archive recovery on a saved cohort only applies to entries whose
            # source snapshot is actually private/deleted.
            if title not in UNAVAILABLE_TITLES:
                continue
        else:
            if title not in UNAVAILABLE_TITLES:
                continue
            if min_position is not None and position < min_position:
                continue
            if max_position is not None and position > max_position:
                continue

        if video_id in cached:
            continue
        result.append(video_id)
        if limit is not None and len(result) >= limit:
            break

    return result
