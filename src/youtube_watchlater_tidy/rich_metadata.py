from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from tqdm import tqdm

from .enrichment import _run_yt_dlp, store_observation
from .llm_store import latest_run_id
from .reports import latest_snapshot_id

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}


@dataclass(frozen=True)
class RichMetadataResult:
    attempted: int
    found: int
    failed: int


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def candidate_video_ids(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    all_videos: bool = False,
    video_ids: list[str] | None = None,
    selection_id: int | None = None,
    missing_description: bool = False,
    llm_needs_description: bool = False,
    run_id: int | None = None,
    remaining: bool = False,
    limit: int | None = None,
    refresh: bool = False,
) -> list[str]:
    explicit = list(dict.fromkeys(video_ids or ()))
    target_count = sum(
        (
            int(all_videos),
            int(bool(explicit)),
            int(selection_id is not None),
            int(missing_description),
            int(llm_needs_description),
        )
    )
    if target_count != 1:
        raise ValueError(
            "choose exactly one metadata target: --all, --video-id, --selection, "
            "--missing-description, or --llm-needs-description"
        )
    if run_id is not None and not llm_needs_description:
        raise ValueError("--run-id is only valid with --llm-needs-description")
    if limit is not None and limit < 0:
        raise ValueError("--limit cannot be negative")

    target_ids: set[str] | None = None
    if selection_id is not None:
        row = conn.execute(
            "SELECT snapshot_id FROM selections WHERE id = ?", (selection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"selection {selection_id} does not exist")
        selected_snapshot = int(row["snapshot_id"])
        if snapshot_id is not None and snapshot_id != selected_snapshot:
            raise ValueError(
                f"selection {selection_id} belongs to snapshot {selected_snapshot}, not {snapshot_id}"
            )
        snapshot_id = selected_snapshot
        target_ids = {
            str(row["video_id"])
            for row in conn.execute(
                "SELECT video_id FROM selection_entries WHERE selection_id = ?",
                (selection_id,),
            )
        }
    elif llm_needs_description:
        if run_id is None:
            run_id = latest_run_id(conn, snapshot_id)
        run = conn.execute(
            "SELECT snapshot_id FROM llm_classification_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"LLM classification run {run_id} does not exist")
        run_snapshot = int(run["snapshot_id"])
        if snapshot_id is not None and snapshot_id != run_snapshot:
            raise ValueError(
                f"LLM classification run {run_id} belongs to snapshot {run_snapshot}, not {snapshot_id}"
            )
        snapshot_id = run_snapshot
        target_ids = {
            str(row["video_id"])
            for row in conn.execute(
                """
                SELECT video_id FROM llm_classifications
                WHERE run_id = ? AND needs_description = 1
                """,
                (run_id,),
            )
        }
    elif snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    assert snapshot_id is not None
    rows = conn.execute(
        """
        SELECT e.position, e.video_id, e.title, e.description AS source_description,
               m.description AS metadata_description,
               d.action AS current_action
        FROM snapshot_entries AS e
        LEFT JOIN preferred_metadata AS m ON m.video_id = e.video_id
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = e.snapshot_id AND d.video_id = e.video_id
        WHERE e.snapshot_id = ?
        ORDER BY e.position
        """,
        (snapshot_id,),
    ).fetchall()
    present = {str(row["video_id"]) for row in rows}

    if explicit:
        missing = [video_id for video_id in explicit if video_id not in present]
        if missing:
            raise ValueError(
                f"video id(s) not present in snapshot {snapshot_id}: {', '.join(missing)}"
            )
        target_ids = set(explicit)

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
        video_id = str(row["video_id"])
        if target_ids is not None and video_id not in target_ids:
            continue
        if remaining and row["current_action"] not in (None, "clear"):
            continue
        unavailable = str(row["title"] or "").casefold() in UNAVAILABLE_TITLES
        if unavailable and not explicit:
            continue
        has_description = _nonempty(row["source_description"]) or _nonempty(
            row["metadata_description"]
        )
        if missing_description and has_description:
            continue
        if llm_needs_description and has_description and not refresh:
            continue
        if video_id in cached:
            continue
        result.append(video_id)
        if limit is not None and len(result) >= limit:
            break
    return result


def enrich_rich_metadata(
    conn: sqlite3.Connection,
    video_ids: list[str],
    *,
    yt_dlp: str = "yt-dlp",
    workers: int = 4,
    start_interval: float = 0.5,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    show_progress: bool = True,
) -> RichMetadataResult:
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    if start_interval < 0:
        raise ValueError("--interval cannot be negative")

    lock = threading.Lock()
    next_start = [time.monotonic()]

    def wait_for_slot() -> None:
        if start_interval == 0:
            return
        with lock:
            now = time.monotonic()
            slot = max(now, next_start[0])
            next_start[0] = slot + start_interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def fetch_one(video_id: str) -> tuple[str, dict[str, Any] | None, Exception | None]:
        wait_for_slot()
        try:
            data = fetcher(video_id) if fetcher is not None else _run_yt_dlp(video_id, yt_dlp=yt_dlp)
            return video_id, data, None
        except Exception as exc:  # fail one video, not the batch
            return video_id, None, exc

    found = 0
    failed = 0
    progress = tqdm(
        total=len(video_ids),
        desc="Rich metadata",
        unit="video",
        disable=not show_progress,
        dynamic_ncols=True,
    )
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_one, video_id): video_id for video_id in video_ids}
            for future in as_completed(futures):
                video_id, data, error = future.result()
                if error is not None:
                    failed += 1
                    store_observation(
                        conn,
                        video_id,
                        "yt-dlp",
                        "error",
                        {"error": str(error)},
                        source_url=f"https://www.youtube.com/watch?v={video_id}",
                    )
                else:
                    assert data is not None
                    found += 1
                    store_observation(conn, video_id, "yt-dlp", "found", data)
                progress.update(1)
                progress.set_postfix_str(f"{video_id} found={found} failed={failed}")
    finally:
        progress.close()

    return RichMetadataResult(attempted=len(video_ids), found=found, failed=failed)
