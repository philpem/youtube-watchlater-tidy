from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .reports import latest_snapshot_id

ACTIONS = ("keep", "review", "archive", "delete", "move")


@dataclass(frozen=True)
class SelectionResult:
    selection_id: int
    snapshot_id: int
    entry_count: int
    selector_type: str


@dataclass(frozen=True)
class SelectionRow:
    position: int
    video_id: str
    title: str
    creator: str
    channel_id: str | None
    duration: float | None
    current_action: str | None
    destination_playlist: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _creator_key(row: sqlite3.Row) -> str:
    return str(
        row["channel_id"]
        or row["uploader_id"]
        or row["channel"]
        or row["uploader"]
        or "(unknown)"
    )


def _rows_for_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    remaining: bool,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT e.position, e.video_id, e.title, e.channel_id, e.channel,
               e.uploader, e.uploader_id, e.duration, e.view_count,
               d.action AS current_action,
               d.destination_playlist
        FROM snapshot_entries AS e
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = e.snapshot_id AND d.video_id = e.video_id
        WHERE e.snapshot_id = ?
        ORDER BY e.position
        """,
        (snapshot_id,),
    ).fetchall()
    if not remaining:
        return rows
    return [row for row in rows if row["current_action"] in (None, "clear")]


def _apply_ranges(
    rows: Iterable[sqlite3.Row],
    *,
    min_duration: float | None,
    max_duration: float | None,
    min_position: int | None,
    max_position: int | None,
) -> list[sqlite3.Row]:
    result: list[sqlite3.Row] = []
    for row in rows:
        duration = row["duration"]
        if min_duration is not None and (duration is None or duration < min_duration):
            continue
        if max_duration is not None and (duration is None or duration > max_duration):
            continue
        if min_position is not None and row["position"] < min_position:
            continue
        if max_position is not None and row["position"] > max_position:
            continue
        result.append(row)
    return result


def _store_selection(
    conn: sqlite3.Connection,
    snapshot_id: int,
    selector_type: str,
    selector: dict[str, Any],
    rows: list[sqlite3.Row],
) -> SelectionResult:
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO selections (snapshot_id, created_at, selector_type, selector_json, entry_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                _utc_now(),
                selector_type,
                json.dumps(selector, ensure_ascii=False, sort_keys=True),
                len(rows),
            ),
        )
        selection_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO selection_entries (selection_id, video_id) VALUES (?, ?)",
            ((selection_id, row["video_id"]) for row in rows),
        )
    return SelectionResult(selection_id, snapshot_id, len(rows), selector_type)


def select_creator(
    conn: sqlite3.Connection,
    creator: str,
    snapshot_id: int | None = None,
    *,
    remaining: bool = False,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_position: int | None = None,
    max_position: int | None = None,
) -> SelectionResult:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    rows = _rows_for_snapshot(conn, snapshot_id, remaining=remaining)

    # Prefer an exact stable creator key. Only fall back to display names if no
    # key matches, and reject ambiguous display names rather than silently
    # combining unrelated channels.
    exact_key_rows = [row for row in rows if _creator_key(row) == creator]
    if exact_key_rows:
        matched = exact_key_rows
    else:
        folded = creator.casefold()
        name_matches: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            names = [row["channel"], row["uploader"], row["uploader_id"]]
            if any(isinstance(name, str) and name.casefold() == folded for name in names):
                name_matches.setdefault(_creator_key(row), []).append(row)
        if len(name_matches) > 1:
            keys = ", ".join(sorted(name_matches))
            raise ValueError(
                f"creator name {creator!r} is ambiguous; use a stable channel/uploader id: {keys}"
            )
        matched = next(iter(name_matches.values()), [])

    matched = _apply_ranges(
        matched,
        min_duration=min_duration,
        max_duration=max_duration,
        min_position=min_position,
        max_position=max_position,
    )
    selector = {
        "creator": creator,
        "remaining": remaining,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "min_position": min_position,
        "max_position": max_position,
    }
    return _store_selection(conn, snapshot_id, "creator", selector, matched)


def select_title(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    regex: str | None = None,
    case_sensitive: bool = False,
    snapshot_id: int | None = None,
    remaining: bool = False,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_position: int | None = None,
    max_position: int | None = None,
) -> SelectionResult:
    if (contains is None) == (regex is None):
        raise ValueError("specify exactly one of contains or regex")
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    rows = _rows_for_snapshot(conn, snapshot_id, remaining=remaining)

    if contains is not None:
        needle = contains if case_sensitive else contains.casefold()

        def title_matches(row: sqlite3.Row) -> bool:
            title = row["title"] or ""
            haystack = title if case_sensitive else title.casefold()
            return needle in haystack

        selector_value: dict[str, Any] = {"contains": contains}
    else:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(regex or "", flags)
        except re.error as exc:
            raise ValueError(f"invalid title regex: {exc}") from exc

        def title_matches(row: sqlite3.Row) -> bool:
            return pattern.search(row["title"] or "") is not None

        selector_value = {"regex": regex}

    matched = [row for row in rows if title_matches(row)]
    matched = _apply_ranges(
        matched,
        min_duration=min_duration,
        max_duration=max_duration,
        min_position=min_position,
        max_position=max_position,
    )
    selector_value.update(
        {
            "case_sensitive": case_sensitive,
            "remaining": remaining,
            "min_duration": min_duration,
            "max_duration": max_duration,
            "min_position": min_position,
            "max_position": max_position,
        }
    )
    return _store_selection(conn, snapshot_id, "title", selector_value, matched)


def latest_selection_id(conn: sqlite3.Connection, snapshot_id: int | None = None) -> int:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    row = conn.execute(
        "SELECT id FROM selections WHERE snapshot_id = ? ORDER BY id DESC LIMIT 1",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"snapshot {snapshot_id} has no selection")
    return int(row["id"])


def selection_rows(
    conn: sqlite3.Connection,
    selection_id: int | None = None,
    *,
    snapshot_id: int | None = None,
) -> list[SelectionRow]:
    if selection_id is None:
        selection_id = latest_selection_id(conn, snapshot_id)
    rows = conn.execute(
        """
        SELECT e.position, e.video_id, e.title, e.channel_id, e.channel, e.uploader,
               e.duration, d.action AS current_action, d.destination_playlist
        FROM selections AS s
        JOIN selection_entries AS se ON se.selection_id = s.id
        JOIN snapshot_entries AS e
          ON e.snapshot_id = s.snapshot_id AND e.video_id = se.video_id
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = e.snapshot_id AND d.video_id = e.video_id
        WHERE s.id = ?
        ORDER BY e.position
        """,
        (selection_id,),
    ).fetchall()
    return [
        SelectionRow(
            position=int(row["position"]),
            video_id=str(row["video_id"]),
            title=row["title"] or "(untitled)",
            creator=row["channel"] or row["uploader"] or "(unknown)",
            channel_id=row["channel_id"],
            duration=float(row["duration"]) if row["duration"] is not None else None,
            current_action=None if row["current_action"] == "clear" else row["current_action"],
            destination_playlist=row["destination_playlist"],
        )
        for row in rows
    ]


def _selection_snapshot(conn: sqlite3.Connection, selection_id: int) -> int:
    row = conn.execute(
        "SELECT snapshot_id FROM selections WHERE id = ?", (selection_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"selection {selection_id} does not exist")
    return int(row["snapshot_id"])


def apply_selection_action(
    conn: sqlite3.Connection,
    action: str,
    *,
    destination_playlist: str | None = None,
    reason: str | None = None,
    selection_id: int | None = None,
    snapshot_id: int | None = None,
    source: str = "human",
) -> int:
    if action not in ACTIONS:
        raise ValueError(f"invalid action {action!r}; expected one of {', '.join(ACTIONS)}")
    if action == "move" and not destination_playlist:
        raise ValueError("move requires --playlist")
    if action != "move" and destination_playlist:
        raise ValueError("--playlist is only valid with move")
    if selection_id is None:
        selection_id = latest_selection_id(conn, snapshot_id)
    selection_snapshot = _selection_snapshot(conn, selection_id)
    if snapshot_id is not None and selection_snapshot != snapshot_id:
        raise ValueError("selection belongs to a different snapshot")

    selected = conn.execute(
        "SELECT video_id FROM selection_entries WHERE selection_id = ? ORDER BY video_id",
        (selection_id,),
    ).fetchall()
    now = _utc_now()
    with conn:
        for row in selected:
            previous = conn.execute(
                "SELECT id FROM current_decisions WHERE snapshot_id = ? AND video_id = ?",
                (selection_snapshot, row["video_id"]),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO decision_events (
                    snapshot_id, video_id, action, destination_playlist,
                    source, rule_json, reason, created_at, supersedes_id
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    selection_snapshot,
                    row["video_id"],
                    action,
                    destination_playlist,
                    source,
                    reason,
                    now,
                    previous["id"] if previous else None,
                ),
            )
    return len(selected)


def undo_selection_action(
    conn: sqlite3.Connection,
    *,
    reason: str | None = None,
    selection_id: int | None = None,
    snapshot_id: int | None = None,
) -> int:
    if selection_id is None:
        selection_id = latest_selection_id(conn, snapshot_id)
    selection_snapshot = _selection_snapshot(conn, selection_id)
    if snapshot_id is not None and selection_snapshot != snapshot_id:
        raise ValueError("selection belongs to a different snapshot")

    selected = conn.execute(
        "SELECT video_id FROM selection_entries WHERE selection_id = ? ORDER BY video_id",
        (selection_id,),
    ).fetchall()
    now = _utc_now()
    changed = 0
    with conn:
        for row in selected:
            previous = conn.execute(
                "SELECT id, action FROM current_decisions WHERE snapshot_id = ? AND video_id = ?",
                (selection_snapshot, row["video_id"]),
            ).fetchone()
            if previous is None or previous["action"] == "clear":
                continue
            conn.execute(
                """
                INSERT INTO decision_events (
                    snapshot_id, video_id, action, destination_playlist,
                    source, rule_json, reason, created_at, supersedes_id
                ) VALUES (?, ?, 'clear', NULL, 'human', NULL, ?, ?, ?)
                """,
                (selection_snapshot, row["video_id"], reason, now, previous["id"]),
            )
            changed += 1
    return changed
