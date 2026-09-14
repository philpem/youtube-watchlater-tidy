from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CreatorRow:
    creator_key: str
    channel_id: str | None
    name: str
    count: int
    total_duration: float
    median_duration: float | None
    median_views: float | None
    first_position: int
    last_position: int


def latest_snapshot_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError("no snapshots have been imported")
    return int(row["id"])


def creator_rows(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
) -> list[CreatorRow]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    rows = conn.execute(
        """
        SELECT position, channel_id, channel, uploader, uploader_id, duration, view_count
        FROM snapshot_entries
        WHERE snapshot_id = ?
        ORDER BY position
        """,
        (snapshot_id,),
    ).fetchall()

    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        key = (
            row["channel_id"]
            or row["uploader_id"]
            or row["channel"]
            or row["uploader"]
            or "(unknown)"
        )
        group = grouped.setdefault(
            str(key),
            {
                "channel_id": row["channel_id"],
                "name": row["channel"] or row["uploader"] or "(unknown)",
                "positions": [],
                "durations": [],
                "views": [],
            },
        )
        group["positions"].append(int(row["position"]))  # type: ignore[union-attr]
        if row["duration"] is not None:
            group["durations"].append(float(row["duration"]))  # type: ignore[union-attr]
        if row["view_count"] is not None:
            group["views"].append(int(row["view_count"]))  # type: ignore[union-attr]

    result: list[CreatorRow] = []
    for key, group in grouped.items():
        positions = group["positions"]
        durations = group["durations"]
        views = group["views"]
        assert isinstance(positions, list)
        assert isinstance(durations, list)
        assert isinstance(views, list)
        result.append(
            CreatorRow(
                creator_key=key,
                channel_id=group["channel_id"] if isinstance(group["channel_id"], str) else None,
                name=str(group["name"]),
                count=len(positions),
                total_duration=sum(durations),
                median_duration=statistics.median(durations) if durations else None,
                median_views=statistics.median(views) if views else None,
                first_position=min(positions),
                last_position=max(positions),
            )
        )

    result.sort(key=lambda item: (-item.count, -item.total_duration, item.name.casefold()))
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


def _fmt_views(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def render_creators(rows: Iterable[CreatorRow], limit: int | None = None) -> str:
    rows = list(rows)
    if limit is not None:
        rows = rows[:limit]

    headers = ["Count", "Total", "Median", "Median views", "Positions", "Creator", "Channel ID"]
    data = [
        [
            str(row.count),
            format_duration(row.total_duration),
            format_duration(row.median_duration),
            _fmt_views(row.median_views),
            f"{row.first_position}-{row.last_position}",
            row.name,
            row.channel_id or "-",
        ]
        for row in rows
    ]

    widths = [len(header) for header in headers]
    for item in data:
        for i, value in enumerate(item):
            widths[i] = max(widths[i], len(value))

    def line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    output = [line(headers), line(["-" * width for width in widths])]
    output.extend(line(item) for item in data)
    return "\n".join(output)
