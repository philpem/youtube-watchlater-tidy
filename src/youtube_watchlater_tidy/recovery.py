from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tqdm import tqdm

from .enrichment import store_observation
from .preservetube import recover_preservetube_metadata
from .reports import latest_snapshot_id

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}
DEFAULT_FINDYOUTUBEVIDEO_BASE = "https://findyoutubevideo.thetechrobo.ca"
BACKEND_NAME = "findyoutubevideo-v5"
FILMOT_SOURCE = "filmot-via-findyoutubevideo"


@dataclass(frozen=True)
class RecoveryResult:
    attempted: int
    found: int
    not_found: int
    failed: int
    metadata_recovered: int


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
    # includeRaw lets metadata-only services such as Filmot return the actual
    # historical metadata they used to reach their verdict. Keeping this in
    # the federated request avoids a second request to Filmot itself.
    api_url = f"{base_url.rstrip('/')}/api/v5/{video_id}?includeRaw=true"
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


def _filmot_item(data: dict[str, Any]) -> dict[str, Any] | None:
    services = data.get("keys")
    if not isinstance(services, list):
        return None

    for service in services:
        if not isinstance(service, dict):
            continue
        identity = str(service.get("classname") or service.get("name") or "").casefold()
        if "filmot" not in identity:
            continue
        raw = service.get("rawraw")
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            return raw[0]
        if isinstance(raw, dict):
            return raw
    return None


def _normalise_filmot(video_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title")
    channel_id = item.get("channelid") or item.get("channel_id")
    channel = item.get("channelname") or item.get("channel")
    description = item.get("description")
    duration = item.get("duration")
    upload_date = item.get("uploaddate") or item.get("upload_date")
    view_count = item.get("view_count")
    if view_count is None:
        view_count = item.get("viewcount")
    if view_count is None:
        view_count = item.get("views")

    # Require at least one genuinely useful field before creating a preferred
    # metadata observation. rawraw sometimes contains bookkeeping only.
    if not any(value not in (None, "") for value in (title, channel_id, channel, description)):
        return None

    return {
        "id": video_id,
        "title": title,
        "description": description,
        "channel_id": channel_id,
        "channel": channel,
        "uploader": channel,
        "duration": duration,
        "view_count": view_count,
        "upload_date": str(upload_date) if upload_date not in (None, "") else None,
        "webpage_url": f"https://filmot.com/video/{video_id}",
        "_filmot_raw": item,
    }


def store_recovered_metadata(
    conn: sqlite3.Connection,
    video_id: str,
    data: dict[str, Any],
) -> bool:
    item = _filmot_item(data)
    if item is None:
        return False
    normalised = _normalise_filmot(video_id, item)
    if normalised is None:
        return False
    store_observation(
        conn,
        video_id,
        FILMOT_SOURCE,
        "found",
        normalised,
        source_url=f"https://filmot.com/video/{video_id}",
    )
    return True


def recover_with_findyoutubevideo(
    conn: sqlite3.Connection,
    video_ids: list[str],
    *,
    base_url: str = DEFAULT_FINDYOUTUBEVIDEO_BASE,
    timeout: float = 90.0,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    preservetube_fetcher: Callable[[str], dict[str, Any] | None] | None = None,
    show_progress: bool = True,
) -> RecoveryResult:
    found = 0
    not_found = 0
    failed = 0
    metadata_recovered = 0

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
        progress.set_postfix_str(
            f"{video_id} found={found} metadata={metadata_recovered} "
            f"missing={not_found} failed={failed}"
        )
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

        # Prefer Filmot metadata carried in the federated response because it
        # requires no extra request. If that yielded nothing, only then ask
        # PreserveTube directly, and only when the finder says it has a copy.
        recovered = store_recovered_metadata(conn, video_id, data)
        if not recovered:
            recovered = recover_preservetube_metadata(
                conn,
                video_id,
                data,
                fetcher=preservetube_fetcher,
            )
        if recovered:
            metadata_recovered += 1

    return RecoveryResult(
        attempted=len(video_ids),
        found=found,
        not_found=not_found,
        failed=failed,
        metadata_recovered=metadata_recovered,
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
