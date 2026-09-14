from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImportResult:
    snapshot_id: int
    entry_count: int
    source_sha256: str
    already_present: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_url(entry: dict[str, Any]) -> str:
    video_id = entry.get("id")
    url = entry.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return f"https://www.youtube.com/watch?v={video_id}"


def import_watchlater_json(conn: sqlite3.Connection, source: str | Path) -> ImportResult:
    source_path = Path(source)
    raw_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    existing = conn.execute(
        "SELECT id, entry_count FROM snapshots WHERE source_sha256 = ?",
        (source_sha256,),
    ).fetchone()
    if existing is not None:
        return ImportResult(
            snapshot_id=existing["id"],
            entry_count=existing["entry_count"],
            source_sha256=source_sha256,
            already_present=True,
        )

    try:
        document = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source_path}: invalid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise ValueError(f"{source_path}: expected a top-level JSON object")

    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{source_path}: expected an 'entries' array")

    imported_at = _utc_now()

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO snapshots (
                source_path, source_sha256, imported_at,
                playlist_id, playlist_title, playlist_modified_date,
                reported_playlist_count, entry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(source_path),
                source_sha256,
                imported_at,
                document.get("id"),
                document.get("title"),
                document.get("modified_date"),
                document.get("playlist_count"),
                len(entries),
            ),
        )
        snapshot_id = int(cursor.lastrowid)

        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"{source_path}: entry {position} is not a JSON object"
                )

            video_id = entry.get("id")
            if not isinstance(video_id, str) or not video_id:
                raise ValueError(
                    f"{source_path}: entry {position} has no usable video id"
                )

            canonical_url = _canonical_url(entry)
            conn.execute(
                """
                INSERT INTO videos (video_id, canonical_url, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    canonical_url = excluded.canonical_url,
                    last_seen_at = excluded.last_seen_at
                """,
                (video_id, canonical_url, imported_at, imported_at),
            )

            thumbnails = entry.get("thumbnails")
            conn.execute(
                """
                INSERT INTO snapshot_entries (
                    snapshot_id, position, video_id, title, description,
                    channel_id, channel, uploader, uploader_id,
                    duration, view_count, availability, timestamp,
                    release_timestamp, thumbnails_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    position,
                    video_id,
                    entry.get("title"),
                    entry.get("description"),
                    entry.get("channel_id"),
                    entry.get("channel"),
                    entry.get("uploader"),
                    entry.get("uploader_id"),
                    entry.get("duration"),
                    entry.get("view_count"),
                    entry.get("availability"),
                    entry.get("timestamp"),
                    entry.get("release_timestamp"),
                    json.dumps(thumbnails, ensure_ascii=False, separators=(",", ":"))
                    if thumbnails is not None
                    else None,
                    json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    return ImportResult(
        snapshot_id=snapshot_id,
        entry_count=len(entries),
        source_sha256=source_sha256,
        already_present=False,
    )
