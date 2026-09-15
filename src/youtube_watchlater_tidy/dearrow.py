from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tqdm import tqdm

from .reports import latest_snapshot_id

DEFAULT_DEARROW_BASE = "https://sponsor.ajay.app"


@dataclass(frozen=True)
class DeArrowResult:
    attempted: int
    found: int
    preferred: int
    not_found: int
    failed: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def candidate_video_ids(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    all_videos: bool = False,
    video_ids: list[str] | None = None,
    selection_id: int | None = None,
    remaining: bool = False,
    limit: int | None = None,
    refresh: bool = False,
    max_age_seconds: float | None = None,
) -> list[str]:
    explicit = list(dict.fromkeys(video_ids or ()))
    target_count = int(all_videos) + int(bool(explicit)) + int(selection_id is not None)
    if target_count != 1:
        raise ValueError("choose exactly one DeArrow target: --all, --video-id, or --selection")
    if remaining and not all_videos:
        raise ValueError("--remaining is only valid with --all")
    if limit is not None and limit < 0:
        raise ValueError("--limit cannot be negative")
    if max_age_seconds is not None and max_age_seconds < 0:
        raise ValueError("--max-age cannot be negative")

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
        SELECT e.position, e.video_id, d.action AS current_action
        FROM snapshot_entries AS e
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
    elif selection_id is not None:
        target_ids = {
            str(row["video_id"])
            for row in conn.execute(
                "SELECT video_id FROM selection_entries WHERE selection_id = ?",
                (selection_id,),
            )
        }
    else:
        target_ids = set()

    cached: set[str] = set()
    if not refresh:
        latest = conn.execute(
            """
            SELECT d.video_id, d.looked_up_at, d.status
            FROM dearrow_lookups AS d
            WHERE d.status IN ('found', 'not_found')
              AND NOT EXISTS (
                  SELECT 1 FROM dearrow_lookups AS newer
                  WHERE newer.video_id = d.video_id
                    AND newer.status IN ('found', 'not_found')
                    AND newer.id > d.id
              )
            """
        ).fetchall()
        cutoff = None
        if max_age_seconds is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        for row in latest:
            if cutoff is None:
                cached.add(str(row["video_id"]))
                continue
            looked_up_at = _parse_time(str(row["looked_up_at"]))
            if looked_up_at is not None and looked_up_at >= cutoff:
                cached.add(str(row["video_id"]))

    result: list[str] = []
    for row in rows:
        video_id = str(row["video_id"])
        action = row["current_action"]
        if explicit or selection_id is not None:
            if video_id not in target_ids:
                continue
        elif remaining and action not in (None, "clear"):
            continue
        if video_id in cached:
            continue
        result.append(video_id)
        if limit is not None and len(result) >= limit:
            break
    return result


def _normalise_titles(data: dict[str, Any]) -> list[dict[str, Any]]:
    titles = data.get("titles")
    if not isinstance(titles, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in titles:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        votes = entry.get("votes")
        try:
            votes_value = int(votes)
        except (TypeError, ValueError):
            votes_value = None
        uuid = entry.get("UUID")
        result.append(
            {
                "title": title,
                "original": entry.get("original") is True,
                "votes": votes_value,
                "locked": entry.get("locked") is True,
                "UUID": uuid if isinstance(uuid, str) else None,
            }
        )
    return result


def preferred_title(titles: list[dict[str, Any]]) -> str | None:
    """Return the trusted first DeArrow alternate title, if there is one.

    DeArrow returns title submissions in preferred/quality order. The first item
    is only accepted when it is locked or has non-negative votes. An
    ``original=true`` item means DeArrow prefers the original YouTube title, so
    it is intentionally not exposed as an alternate title.
    """
    if not titles:
        return None
    first = titles[0]
    if first.get("original") is True:
        return None
    votes = first.get("votes")
    trusted = first.get("locked") is True or (
        isinstance(votes, int) and votes >= 0
    )
    if not trusted:
        return None
    title = first.get("title")
    return str(title) if isinstance(title, str) and title else None


def fetch_dearrow(
    video_id: str,
    *,
    base_url: str = DEFAULT_DEARROW_BASE,
    timeout: float = 15.0,
    hash_prefix: bool = False,
) -> dict[str, Any] | None:
    if hash_prefix:
        prefix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]
        url = f"{base_url.rstrip('/')}/api/branding/{prefix}?{urlencode({'fetchAll': 'true'})}"
    else:
        url = f"{base_url.rstrip('/')}/api/branding?{urlencode({'videoID': video_id, 'fetchAll': 'true'})}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "youtube-watchlater-tidy/0.1 (+https://github.com/philpem/youtube-watchlater-tidy)",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 429:
            raise RuntimeError("DeArrow rate limit (HTTP 429); retry later") from exc
        raise RuntimeError(f"HTTP {exc.code} from DeArrow") from exc
    except URLError as exc:
        raise RuntimeError(f"DeArrow request failed: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DeArrow returned invalid JSON for {video_id}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"DeArrow returned non-object JSON for {video_id}")

    if hash_prefix:
        item = data.get(video_id)
        if item is None:
            return None
        if not isinstance(item, dict):
            raise RuntimeError(f"DeArrow hash-prefix response for {video_id} is not an object")
        return item
    return data


def _source_url(video_id: str, base_url: str, *, hash_prefix: bool) -> str:
    if hash_prefix:
        prefix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]
        return f"{base_url.rstrip('/')}/api/branding/{prefix}?fetchAll=true"
    return f"{base_url.rstrip('/')}/api/branding?{urlencode({'videoID': video_id, 'fetchAll': 'true'})}"


def _store_lookup(
    conn: sqlite3.Connection,
    video_id: str,
    status: str,
    data: dict[str, Any],
    *,
    base_url: str,
    hash_prefix: bool,
) -> tuple[bool, bool]:
    titles = _normalise_titles(data)
    preferred = preferred_title(titles)
    source_url = _source_url(video_id, base_url, hash_prefix=hash_prefix)
    with conn:
        conn.execute(
            """
            INSERT INTO dearrow_lookups (
                video_id, looked_up_at, status, preferred_title,
                titles_json, source_url, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                _utc_now(),
                status,
                preferred,
                json.dumps(titles, ensure_ascii=False, separators=(",", ":")),
                source_url,
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            ),
        )
    return bool(titles), preferred is not None


def enrich_dearrow(
    conn: sqlite3.Connection,
    video_ids: list[str],
    *,
    base_url: str = DEFAULT_DEARROW_BASE,
    timeout: float = 15.0,
    hash_prefix: bool = False,
    workers: int = 4,
    fetcher: Callable[[str], dict[str, Any] | None] | None = None,
    show_progress: bool = True,
) -> DeArrowResult:
    if workers < 1:
        raise ValueError("workers must be at least 1")

    found = 0
    preferred = 0
    not_found = 0
    failed = 0

    def fetch_one(video_id: str) -> tuple[str, dict[str, Any] | None, Exception | None]:
        try:
            if fetcher is not None:
                data = fetcher(video_id)
            else:
                data = fetch_dearrow(
                    video_id,
                    base_url=base_url,
                    timeout=timeout,
                    hash_prefix=hash_prefix,
                )
            return video_id, data, None
        except Exception as exc:  # per-video failure; retain and continue
            return video_id, None, exc

    progress = tqdm(
        total=len(video_ids),
        desc="DeArrow",
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
                    _store_lookup(
                        conn,
                        video_id,
                        "error",
                        {"error": str(error)},
                        base_url=base_url,
                        hash_prefix=hash_prefix,
                    )
                elif data is None:
                    not_found += 1
                    _store_lookup(
                        conn,
                        video_id,
                        "not_found",
                        {},
                        base_url=base_url,
                        hash_prefix=hash_prefix,
                    )
                else:
                    titles = _normalise_titles(data)
                    has_titles, has_preferred = _store_lookup(
                        conn,
                        video_id,
                        "found" if titles else "not_found",
                        data,
                        base_url=base_url,
                        hash_prefix=hash_prefix,
                    )
                    if has_titles:
                        found += 1
                    else:
                        not_found += 1
                    if has_preferred:
                        preferred += 1
                progress.update(1)
                progress.set_postfix_str(
                    f"{video_id} found={found} preferred={preferred} missing={not_found} failed={failed}"
                )
    finally:
        progress.close()

    return DeArrowResult(
        attempted=len(video_ids),
        found=found,
        preferred=preferred,
        not_found=not_found,
        failed=failed,
    )


def latest_lookup(conn: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM dearrow_lookups WHERE video_id = ? ORDER BY id DESC LIMIT 1",
        (video_id,),
    ).fetchone()


def preferred_for_video(conn: sqlite3.Connection, video_id: str) -> str | None:
    """Return the preferred title from the latest completed DeArrow lookup.

    Errors do not invalidate a prior good cache entry, but a newer successful
    found/not-found result with no trusted alternate does. This prevents a stale
    preferred title surviving after DeArrow changes its recommendation.
    """
    row = conn.execute(
        """
        SELECT preferred_title
        FROM dearrow_lookups
        WHERE video_id = ? AND status IN ('found', 'not_found')
        ORDER BY id DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()
    return None if row is None else row["preferred_title"]
