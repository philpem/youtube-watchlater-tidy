from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from tqdm import tqdm

from .reports import latest_snapshot_id

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}


@dataclass(frozen=True)
class EnrichmentResult:
    attempted: int
    found: int
    failed: int
    skipped: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _missing_creator(row: sqlite3.Row) -> bool:
    return not any(
        isinstance(row[field], str) and row[field].strip()
        for field in ("channel_id", "uploader_id", "channel", "uploader")
    )


def candidate_video_ids(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    missing_creator: bool = False,
    video_id: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
) -> list[str]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    rows = conn.execute(
        """
        SELECT video_id, title, channel_id, channel, uploader, uploader_id
        FROM snapshot_entries
        WHERE snapshot_id = ?
        ORDER BY position
        """,
        (snapshot_id,),
    ).fetchall()

    cached: set[str] = set()
    if not refresh:
        cached = {
            str(row["video_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT video_id
                FROM metadata_observations
                WHERE source = 'yt-dlp' AND status = 'found'
                """
            )
        }

    result: list[str] = []
    for row in rows:
        row_video_id = str(row["video_id"])
        if video_id is not None and row_video_id != video_id:
            continue
        if row_video_id in cached:
            continue
        if missing_creator:
            if not _missing_creator(row):
                continue
            if (row["title"] or "").casefold() in UNAVAILABLE_TITLES:
                continue
        result.append(row_video_id)
        if limit is not None and len(result) >= limit:
            break

    if video_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM snapshot_entries WHERE snapshot_id = ? AND video_id = ?",
            (snapshot_id, video_id),
        ).fetchone()
        if exists is None:
            raise ValueError(f"video {video_id!r} is not present in snapshot {snapshot_id}")

    return result


def _run_yt_dlp(video_id: str, *, yt_dlp: str = "yt-dlp") -> dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    proc = subprocess.run(
        [yt_dlp, "--skip-download", "--dump-single-json", "--no-warnings", url],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or f"yt-dlp exited {proc.returncode}"
        raise RuntimeError(message)

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid JSON for {video_id}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"yt-dlp returned non-object JSON for {video_id}")

    returned_id = data.get("id")
    if returned_id and returned_id != video_id:
        raise RuntimeError(
            f"yt-dlp returned video {returned_id!r} while enriching {video_id!r}"
        )
    return data


def store_observation(
    conn: sqlite3.Connection,
    video_id: str,
    source: str,
    status: str,
    raw: dict[str, Any],
    *,
    exact_match: bool = True,
    source_url: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO metadata_observations (
                video_id, source, observed_at, status, exact_match,
                title, description, channel_id, channel, uploader, uploader_id,
                duration, view_count, upload_date, timestamp, availability,
                source_url, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                source,
                _utc_now(),
                status,
                1 if exact_match else 0,
                raw.get("title"),
                raw.get("description"),
                raw.get("channel_id"),
                raw.get("channel"),
                raw.get("uploader"),
                raw.get("uploader_id"),
                raw.get("duration"),
                raw.get("view_count"),
                raw.get("upload_date"),
                raw.get("timestamp"),
                raw.get("availability"),
                source_url or raw.get("webpage_url"),
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            ),
        )


def enrich_with_ytdlp(
    conn: sqlite3.Connection,
    video_ids: list[str],
    *,
    yt_dlp: str = "yt-dlp",
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    show_progress: bool = False,
) -> EnrichmentResult:
    attempted = 0
    found = 0
    failed = 0

    if fetcher is None:
        fetcher = lambda vid: _run_yt_dlp(vid, yt_dlp=yt_dlp)

    iterator = tqdm(
        video_ids,
        desc="Enriching",
        unit="video",
        dynamic_ncols=True,
        disable=not show_progress,
    )

    for video_id in iterator:
        attempted += 1
        iterator.set_postfix_str(f"{video_id} found={found} failed={failed}", refresh=True)
        try:
            data = fetcher(video_id)
        except Exception as exc:
            failed += 1
            store_observation(
                conn,
                video_id,
                "yt-dlp",
                "error",
                {"error": str(exc)},
                source_url=f"https://www.youtube.com/watch?v={video_id}",
            )
            iterator.set_postfix_str(f"{video_id} failed found={found} failed={failed}")
            continue

        found += 1
        store_observation(conn, video_id, "yt-dlp", "found", data)
        creator = data.get("channel") or data.get("uploader") or "unknown creator"
        iterator.set_postfix_str(f"{video_id} {creator} found={found} failed={failed}")

    return EnrichmentResult(
        attempted=attempted,
        found=found,
        failed=failed,
        skipped=0,
    )


def latest_found_observation(
    conn: sqlite3.Connection,
    video_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM metadata_observations
        WHERE video_id = ? AND status = 'found'
        ORDER BY id DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()
