from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .enrichment import store_observation

DEFAULT_PRESERVETUBE_API_BASE = "https://api.preservetube.com"
PRESERVETUBE_SOURCE = "preservetube-via-findyoutubevideo"


def _service_entry(data: dict[str, Any]) -> dict[str, Any] | None:
    services = data.get("keys")
    if not isinstance(services, list):
        return None

    for service in services:
        if not isinstance(service, dict):
            continue
        identity = str(service.get("classname") or service.get("name") or "").casefold()
        if "preservetube" in identity:
            return service
    return None


def finder_says_preservetube_has_video(data: dict[str, Any]) -> bool:
    service = _service_entry(data)
    if service is None:
        return False
    if service.get("archived") is True:
        return True
    available = service.get("available")
    return isinstance(available, list) and bool(available)


def fetch_preservetube(
    video_id: str,
    *,
    base_url: str = DEFAULT_PRESERVETUBE_API_BASE,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    api_url = f"{base_url.rstrip('/')}/video/{video_id}"
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
        if exc.code == 404:
            return None
        raise RuntimeError(f"HTTP {exc.code} from PreserveTube") from exc
    except URLError as exc:
        raise RuntimeError(f"PreserveTube request failed: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PreserveTube returned invalid JSON for {video_id}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"PreserveTube returned non-object JSON for {video_id}")

    if data.get("error"):
        return None

    returned_id = data.get("id")
    if returned_id and returned_id != video_id:
        raise RuntimeError(
            f"PreserveTube returned video {returned_id!r} while recovering {video_id!r}"
        )
    return data


def normalise_preservetube(video_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title")
    description = item.get("description")
    channel = item.get("channel") or item.get("channelName")
    channel_id = item.get("channelId") or item.get("channel_id")
    published = item.get("published") or item.get("upload_date")

    if not any(value not in (None, "") for value in (title, description, channel, channel_id)):
        return None

    return {
        "id": video_id,
        "title": title,
        "description": description,
        "channel_id": channel_id,
        "channel": channel,
        "uploader": channel,
        "upload_date": str(published) if published not in (None, "") else None,
        "webpage_url": f"https://preservetube.com/watch?v={video_id}",
        "_preservetube_raw": item,
    }


def recover_preservetube_metadata(
    conn: sqlite3.Connection,
    video_id: str,
    finder_data: dict[str, Any],
    *,
    base_url: str = DEFAULT_PRESERVETUBE_API_BASE,
    timeout: float = 15.0,
    fetcher: Callable[[str], dict[str, Any] | None] | None = None,
) -> bool:
    if not finder_says_preservetube_has_video(finder_data):
        return False

    if fetcher is None:
        fetcher = lambda vid: fetch_preservetube(vid, base_url=base_url, timeout=timeout)

    source_url = f"https://preservetube.com/watch?v={video_id}"
    try:
        item = fetcher(video_id)
    except Exception as exc:
        store_observation(
            conn,
            video_id,
            PRESERVETUBE_SOURCE,
            "error",
            {"error": str(exc)},
            source_url=source_url,
        )
        return False

    if item is None:
        store_observation(
            conn,
            video_id,
            PRESERVETUBE_SOURCE,
            "not_found",
            {},
            source_url=source_url,
        )
        return False

    returned_id = item.get("id")
    if returned_id and returned_id != video_id:
        store_observation(
            conn,
            video_id,
            PRESERVETUBE_SOURCE,
            "error",
            {"error": f"PreserveTube returned video {returned_id!r}"},
            source_url=source_url,
        )
        return False

    normalised = normalise_preservetube(video_id, item)
    if normalised is None:
        store_observation(
            conn,
            video_id,
            PRESERVETUBE_SOURCE,
            "not_found",
            item,
            source_url=source_url,
        )
        return False

    store_observation(
        conn,
        video_id,
        PRESERVETUBE_SOURCE,
        "found",
        normalised,
        source_url=source_url,
    )
    return True
