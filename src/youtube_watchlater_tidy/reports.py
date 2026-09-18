from __future__ import annotations

import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .creator_metadata import creator_associations, primary_creator_index


UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}


@dataclass(frozen=True)
class CreatorRow:
    creator_key: str
    channel_id: str | None
    name: str
    count: int
    unresolved_count: int
    action_counts: dict[str, int]
    total_duration: float
    median_duration: float | None
    median_views: float | None
    first_position: int
    last_position: int


@dataclass(frozen=True)
class VideoRow:
    position: int
    video_id: str
    title: str
    creator: str
    duration: float | None
    view_count: int | None
    availability: str | None
    metadata_source: str | None
    current_action: str | None
    destination_playlist: str | None


def latest_snapshot_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError("no snapshots have been imported")
    return int(row["id"])


def _effective_rows(
    conn: sqlite3.Connection,
    snapshot_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.position, e.video_id, e.title AS source_title,
               CASE
                   WHEN e.title IN ('[Private video]', '[Deleted video]')
                        AND m.title IS NOT NULL
                   THEN m.title
                   ELSE e.title
               END AS title,
               COALESCE(e.channel_id, m.channel_id) AS channel_id,
               COALESCE(e.channel, m.channel) AS channel,
               COALESCE(e.uploader, m.uploader) AS uploader,
               COALESCE(e.uploader_id, m.uploader_id) AS uploader_id,
               COALESCE(e.duration, m.duration) AS duration,
               COALESCE(e.view_count, m.view_count) AS view_count,
               COALESCE(e.availability, m.availability) AS availability,
               e.raw_json AS source_raw_json,
               m.raw_json AS metadata_raw_json,
               m.source AS metadata_source,
               d.action AS current_action,
               d.destination_playlist
        FROM snapshot_entries AS e
        LEFT JOIN preferred_metadata AS m
          ON m.video_id = e.video_id
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = e.snapshot_id AND d.video_id = e.video_id
        WHERE e.snapshot_id = ?
        ORDER BY e.position
        """,
        (snapshot_id,),
    ).fetchall()


def creator_rows(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    remaining: bool = False,
) -> list[CreatorRow]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    all_rows = _effective_rows(conn, snapshot_id)
    names_to_keys, primary_details = primary_creator_index(all_rows)

    rows = all_rows
    if remaining:
        rows = [row for row in rows if row["current_action"] in (None, "clear")]
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        for association in creator_associations(
            row,
            names_to_keys=names_to_keys,
            primary_details=primary_details,
        ):
            group = grouped.setdefault(
                association.key,
                {
                    "channel_id": association.channel_id,
                    "name": association.name,
                    "positions": [],
                    "durations": [],
                    "views": [],
                    "actions": [],
                },
            )
            group["positions"].append(int(row["position"]))  # type: ignore[union-attr]
            if row["duration"] is not None:
                group["durations"].append(float(row["duration"]))  # type: ignore[union-attr]
            if row["view_count"] is not None:
                group["views"].append(int(row["view_count"]))  # type: ignore[union-attr]
            action = row["current_action"]
            group["actions"].append(None if action == "clear" else action)  # type: ignore[union-attr]

    result: list[CreatorRow] = []
    for key, group in grouped.items():
        positions = group["positions"]
        durations = group["durations"]
        views = group["views"]
        actions = group["actions"]
        assert isinstance(positions, list)
        assert isinstance(durations, list)
        assert isinstance(views, list)
        assert isinstance(actions, list)
        action_counts = Counter(action for action in actions if action is not None)
        result.append(
            CreatorRow(
                creator_key=key,
                channel_id=group["channel_id"] if isinstance(group["channel_id"], str) else None,
                name=str(group["name"]),
                count=len(positions),
                unresolved_count=sum(action is None for action in actions),
                action_counts=dict(action_counts),
                total_duration=sum(durations),
                median_duration=statistics.median(durations) if durations else None,
                median_views=statistics.median(views) if views else None,
                first_position=min(positions),
                last_position=max(positions),
            )
        )

    result.sort(key=lambda item: (-item.count, -item.total_duration, item.name.casefold()))
    return result


def video_rows(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    remaining: bool = False,
    unknown_creator: bool = False,
    unavailable: bool = False,
    recovered: bool | None = None,
) -> list[VideoRow]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    rows = _effective_rows(conn, snapshot_id)

    result: list[VideoRow] = []
    for row in rows:
        action = None if row["current_action"] == "clear" else row["current_action"]
        if remaining and action is not None:
            continue

        source_title = (row["source_title"] or "").casefold()
        source_unavailable = source_title in UNAVAILABLE_TITLES
        if unavailable and not source_unavailable:
            continue

        has_recovered_metadata = row["metadata_source"] is not None
        if recovered is True and not has_recovered_metadata:
            continue
        if recovered is False and has_recovered_metadata:
            continue

        has_creator = any(
            isinstance(row[field], str) and row[field].strip()
            for field in ("channel_id", "uploader_id", "channel", "uploader")
        )
        if unknown_creator and has_creator:
            continue

        result.append(
            VideoRow(
                position=int(row["position"]),
                video_id=str(row["video_id"]),
                title=row["title"] or "(untitled)",
                creator=row["channel"] or row["uploader"] or "(unknown)",
                duration=float(row["duration"]) if row["duration"] is not None else None,
                view_count=int(row["view_count"]) if row["view_count"] is not None else None,
                availability=row["availability"],
                metadata_source=row["metadata_source"],
                current_action=action,
                destination_playlist=row["destination_playlist"],
            )
        )
    return result


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _fmt_views(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def _fmt_actions(actions: dict[str, int]) -> str:
    if not actions:
        return "-"
    return ",".join(f"{key}:{actions[key]}" for key in sorted(actions))


def _table(headers: list[str], data: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for item in data:
        for i, value in enumerate(item):
            widths[i] = max(widths[i], len(value))

    def line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    output = [line(headers), line(["-" * width for width in widths])]
    output.extend(line(item) for item in data)
    return "\n".join(output)


def render_creators(rows: Iterable[CreatorRow], limit: int | None = None) -> str:
    rows = list(rows)
    total = len(rows)
    if limit is not None:
        rows = rows[:limit]

    headers = [
        "Count", "Unresolved", "Total", "Median", "Median views",
        "Positions", "Actions", "Creator", "Channel ID",
    ]
    data = [
        [
            str(row.count),
            str(row.unresolved_count),
            format_duration(row.total_duration),
            format_duration(row.median_duration),
            _fmt_views(row.median_views),
            f"{row.first_position}-{row.last_position}",
            _fmt_actions(row.action_counts),
            row.name,
            row.channel_id or "-",
        ]
        for row in rows
    ]
    output = _table(headers, data)
    if limit is not None and total > limit:
        output += f"\n... {total - limit} more"
    output += f"\n{total} creator(s)"
    return output


def render_videos(rows: Iterable[VideoRow], limit: int | None = None) -> str:
    rows = list(rows)
    total = len(rows)
    if limit is not None:
        rows = rows[:limit]

    headers = [
        "Pos", "Duration", "Views", "Availability", "Metadata", "Action",
        "Creator", "Title", "Video ID",
    ]
    data: list[list[str]] = []
    for row in rows:
        action = row.current_action or "-"
        if row.destination_playlist:
            action = f"{action}->{row.destination_playlist}"
        data.append(
            [
                str(row.position),
                format_duration(row.duration),
                _fmt_views(row.view_count),
                row.availability or "-",
                row.metadata_source or "-",
                action,
                row.creator,
                row.title,
                row.video_id,
            ]
        )
    output = _table(headers, data)
    if limit is not None and total > limit:
        output += f"\n... {total - limit} more"
    output += f"\n{total} video(s)"
    return output
