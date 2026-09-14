from __future__ import annotations

import csv
import io
import json
import sqlite3

from .triage import selection_rows


def _selection_metadata(conn: sqlite3.Connection, selection_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, snapshot_id, created_at, selector_type, selector_json, entry_count
        FROM selections
        WHERE id = ?
        """,
        (selection_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"selection {selection_id} does not exist")
    return row


def selection_export_text(
    conn: sqlite3.Connection,
    selection_id: int,
    *,
    format: str,
) -> str:
    metadata = _selection_metadata(conn, selection_id)
    rows = selection_rows(conn, selection_id)

    videos = [
        {
            "position": row.position,
            "video_id": row.video_id,
            "title": row.title,
            "creator": row.creator,
            "channel_id": row.channel_id,
            "duration": row.duration,
            "action": row.current_action,
            "destination_playlist": row.destination_playlist,
        }
        for row in rows
    ]

    if format == "json":
        payload = {
            "selection_id": int(metadata["id"]),
            "snapshot_id": int(metadata["snapshot_id"]),
            "created_at": metadata["created_at"],
            "selector_type": metadata["selector_type"],
            "selector": json.loads(metadata["selector_json"]),
            "entry_count": int(metadata["entry_count"]),
            "videos": videos,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    if format == "csv":
        output = io.StringIO(newline="")
        fields = [
            "position",
            "video_id",
            "title",
            "creator",
            "channel_id",
            "duration",
            "action",
            "destination_playlist",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(videos)
        return output.getvalue()

    raise ValueError("selection export format must be json or csv")
