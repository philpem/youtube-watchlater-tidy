from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tqdm import tqdm

from .reports import latest_snapshot_id

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}
DEFAULT_FINDYOUTUBEVIDEO_BASE = "https://findyoutubevideo.thetechrobo.ca"
BACKEND_NAME = "findyoutubevideo-v5"


@dataclass(frozen=True)
class RecoveryResult:
    attempted: int
    found: int
    not_found: int
    failed: int


@dataclass(frozen=True)
class ArchiveLink:
    service: str
    title: str
    url: str
    contains: str
    note: str | None
    maybe_paywalled: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def candidate_unavailable_video_ids(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    video_id: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
) -> list[str]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    rows = conn.execute(
        """
        SELECT video_id, title
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
                SELECT video_id
                FROM archive_lookups
                WHERE backend = ? AND status IN ('found', 'not_found')
                GROUP BY video_id
                """,
                (BACKEND_NAME,),
            )
        }

    result: list[str] = []
    matched_requested_id = video_id is None
    for row in rows:
        row_video_id = str(row["video_id"])
        if video_id is not None:
            if row_video_id != video_id:
                continue
            matched_requested_id = True
        elif (row["title"] or "").casefold() not in UNAVAILABLE_TITLES:
            continue

        if row_video_id in cached:
            continue
        result.append(row_video_id)
        if limit is not None and len(result) >= limit:
            break

    if not matched_requested_id:
        raise ValueError(f"video {video_id!r} is not present in snapshot {snapshot_id}")

    return result


def _fetch_findyoutubevideo(
    video_id: str,
    *,
    base_url: str = DEFAULT_FINDYOUTUBEVIDEO_BASE,
    timeout: float = 90.0,
) -> dict[str, Any]:
    api_url = f"{base_url.rstrip('/')}/api/v5/{video_id}"
    request = Request(
        api_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "youtube-watchlater-tidy/0.1 (+https://github.com/philpem/youtube-watchlater-tidy)",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from FindYouTubeVideo") from exc
    except URLError as exc:
        raise RuntimeError(f"FindYouTubeVideo request failed: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FindYouTubeVideo returned invalid JSON for {video_id}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"FindYouTubeVideo returned non-object JSON for {video_id}")

    returned_id = data.get("id")
    if returned_id and returned_id != video_id:
        raise RuntimeError(
            f"FindYouTubeVideo returned video {returned_id!r} while recovering {video_id!r}"
        )
    if data.get("status") == "bad.id":
        raise RuntimeError(f"FindYouTubeVideo rejected video id {video_id!r}")
    return data


def _is_found(data: dict[str, Any]) -> bool:
    verdict = data.get("verdict")
    if isinstance(verdict, dict) and any(
        verdict.get(key) is True for key in ("video", "metaonly", "comments")
    ):
        return True

    keys = data.get("keys")
    if isinstance(keys, list):
        for service in keys:
            if not isinstance(service, dict):
                continue
            if service.get("archived") is True:
                return True
            available = service.get("available")
            if isinstance(available, list) and available:
                return True
    return False


def _human_verdict(data: dict[str, Any]) -> str | None:
    verdict = data.get("verdict")
    if isinstance(verdict, dict):
        value = verdict.get("human_friendly")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _store_lookup(
    conn: sqlite3.Connection,
    video_id: str,
    status: str,
    raw: dict[str, Any],
    *,
    source_url: str,
) -> None:
    verdict = raw.get("verdict") if isinstance(raw.get("verdict"), dict) else {}
    with conn:
        conn.execute(
            """
            INSERT INTO archive_lookups (
                video_id, backend, looked_up_at, status,
                has_video, has_metadata, has_comments,
                human_verdict, source_url, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                BACKEND_NAME,
                _utc_now(),
                status,
                1 if verdict.get("video") is True else 0,
                1 if verdict.get("metaonly") is True else 0,
                1 if verdict.get("comments") is True else 0,
                _human_verdict(raw),
                source_url,
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            ),
        )


def recover_with_findyoutubevideo(
    conn: sqlite3.Connection,
    video_ids: list[str],
    *,
    base_url: str = DEFAULT_FINDYOUTUBEVIDEO_BASE,
    timeout: float = 90.0,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    show_progress: bool = True,
) -> RecoveryResult:
    found = 0
    not_found = 0
    failed = 0

    if fetcher is None:
        fetcher = lambda vid: _fetch_findyoutubevideo(
            vid,
            base_url=base_url,
            timeout=timeout,
        )

    progress = tqdm(
        video_ids,
        desc="Archive recovery",
        unit="video",
        disable=not show_progress,
        dynamic_ncols=True,
    )
    for video_id in progress:
        progress.set_postfix_str(f"{video_id} found={found} missing={not_found} failed={failed}")
        source_url = f"{base_url.rstrip('/')}/?q={video_id}"
        try:
            data = fetcher(video_id)
            returned_id = data.get("id") if isinstance(data, dict) else None
            if returned_id and returned_id != video_id:
                raise RuntimeError(
                    f"FindYouTubeVideo returned video {returned_id!r} while recovering {video_id!r}"
                )
        except Exception as exc:
            failed += 1
            _store_lookup(
                conn,
                video_id,
                "error",
                {"error": str(exc)},
                source_url=source_url,
            )
            continue

        if _is_found(data):
            found += 1
            status = "found"
        else:
            not_found += 1
            status = "not_found"
        _store_lookup(conn, video_id, status, data, source_url=source_url)

    return RecoveryResult(
        attempted=len(video_ids),
        found=found,
        not_found=not_found,
        failed=failed,
    )


def latest_archive_lookup(
    conn: sqlite3.Connection,
    video_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM archive_lookups
        WHERE video_id = ? AND backend = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (video_id, BACKEND_NAME),
    ).fetchone()


def archive_links(raw: dict[str, Any]) -> list[ArchiveLink]:
    result: list[ArchiveLink] = []
    keys = raw.get("keys")
    if not isinstance(keys, list):
        return result

    for service in keys:
        if not isinstance(service, dict):
            continue
        service_name = str(service.get("name") or service.get("classname") or "(unknown service)")
        maybe_paywalled = service.get("maybe_paywalled") is True
        available = service.get("available")
        if not isinstance(available, list):
            continue
        for link in available:
            if not isinstance(link, dict):
                continue
            url = link.get("url")
            if not isinstance(url, str) or not url:
                continue
            contains = link.get("contains")
            if isinstance(contains, str):
                contains_text = contains
            else:
                contains_text = json.dumps(contains, ensure_ascii=False, separators=(",", ":"))
            note = link.get("note")
            result.append(
                ArchiveLink(
                    service=service_name,
                    title=str(link.get("title") or "archived resource"),
                    url=url,
                    contains=contains_text,
                    note=note if isinstance(note, str) else None,
                    maybe_paywalled=maybe_paywalled,
                )
            )
    return result
