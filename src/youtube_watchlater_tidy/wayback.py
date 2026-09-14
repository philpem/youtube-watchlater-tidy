from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from .enrichment import store_observation

WAYBACK_SOURCE = "wayback-via-findyoutubevideo"


def _service_entry(data: dict[str, Any]) -> dict[str, Any] | None:
    services = data.get("keys")
    if not isinstance(services, list):
        return None
    for service in services:
        if not isinstance(service, dict):
            continue
        identity = str(service.get("classname") or service.get("name") or "").casefold()
        if "wayback" in identity:
            return service
    return None


def _link_has_metadata(link: dict[str, Any]) -> bool:
    contains = link.get("contains")
    if isinstance(contains, dict):
        return contains.get("metadata") is True
    if isinstance(contains, list):
        return any(str(item).casefold() == "metadata" for item in contains)
    if isinstance(contains, str):
        return "metadata" in contains.casefold()
    return False


def wayback_watch_url(data: dict[str, Any]) -> str | None:
    service = _service_entry(data)
    if service is None:
        return None
    available = service.get("available")
    if not isinstance(available, list):
        return None

    candidates: list[tuple[int, str]] = []
    for link in available:
        if not isinstance(link, dict):
            continue
        url = link.get("url")
        if not isinstance(url, str) or not url:
            continue
        title = str(link.get("title") or "").casefold()
        score = 0
        if _link_has_metadata(link):
            score += 4
        if "watch page" in title:
            score += 3
        if "youtube.com/watch" in url:
            score += 2
        if score:
            candidates.append((score, url))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def fetch_wayback_page(url: str, *, timeout: float = 20.0) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "youtube-watchlater-tidy/0.1 (+https://github.com/philpem/youtube-watchlater-tidy)",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from Wayback Machine") from exc
    except URLError as exc:
        raise RuntimeError(f"Wayback Machine request failed: {exc.reason}") from exc
    return payload.decode(charset, errors="replace")


def _balanced_json(text: str, start: int) -> dict[str, Any] | None:
    brace = text.find("{", start)
    if brace < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for pos in range(brace, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[brace : pos + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _player_response(html: str) -> dict[str, Any] | None:
    markers = (
        "ytInitialPlayerResponse =",
        "var ytInitialPlayerResponse =",
        "window['ytInitialPlayerResponse'] =",
        'window["ytInitialPlayerResponse"] =',
    )
    for marker in markers:
        offset = html.find(marker)
        if offset >= 0:
            parsed = _balanced_json(html, offset + len(marker))
            if parsed is not None:
                return parsed

    # Older watch pages often embedded player_response as a JSON string inside
    # ytplayer.config. Parse that object, then decode args.player_response.
    for marker in ("ytplayer.config =", "ytplayer.config = "):
        offset = html.find(marker)
        if offset < 0:
            continue
        config = _balanced_json(html, offset + len(marker))
        if not isinstance(config, dict):
            continue
        args = config.get("args")
        if not isinstance(args, dict):
            continue
        raw = args.get("player_response")
        if not isinstance(raw, str):
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _meta_content(soup: BeautifulSoup, selectors: tuple[tuple[str, str], ...]) -> str | None:
    for attr, value in selectors:
        tag = soup.find(attrs={attr: value})
        if tag is None:
            continue
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _clean_youtube_title(value: str | None) -> str | None:
    if not value:
        return None
    title = value.strip()
    if title.casefold().endswith(" - youtube"):
        title = title[: -len(" - YouTube")].rstrip()
    return title or None


def normalise_wayback(video_id: str, html: str, source_url: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    player = _player_response(html)
    details = player.get("videoDetails") if isinstance(player, dict) else None
    if not isinstance(details, dict):
        details = {}

    title = details.get("title")
    if not isinstance(title, str) or not title.strip():
        title = _meta_content(
            soup,
            (
                ("property", "og:title"),
                ("name", "title"),
                ("itemprop", "name"),
            ),
        )
    if not title and soup.title and soup.title.string:
        title = soup.title.string
    title = _clean_youtube_title(title if isinstance(title, str) else None)

    description = details.get("shortDescription")
    if not isinstance(description, str) or not description.strip():
        description = _meta_content(
            soup,
            (("property", "og:description"), ("name", "description")),
        )

    channel = details.get("author")
    if not isinstance(channel, str) or not channel.strip():
        channel = _meta_content(soup, (("itemprop", "author"),))
        if channel is None:
            author = soup.find(attrs={"itemprop": "author"})
            if author is not None:
                named = author.find(attrs={"itemprop": "name"})
                if named is not None:
                    channel = named.get("content") or named.get_text(strip=True)

    channel_id = details.get("channelId")
    if not isinstance(channel_id, str) or not channel_id.strip():
        channel_id = _meta_content(soup, (("itemprop", "channelId"),))

    upload_date = _meta_content(soup, (("itemprop", "datePublished"),))

    duration: float | None = None
    raw_duration = details.get("lengthSeconds")
    try:
        if raw_duration not in (None, ""):
            duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = None

    view_count: int | None = None
    raw_views = details.get("viewCount")
    try:
        if raw_views not in (None, ""):
            view_count = int(raw_views)
    except (TypeError, ValueError):
        view_count = None

    if not any(value not in (None, "") for value in (title, description, channel, channel_id)):
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
        "upload_date": upload_date,
        "webpage_url": source_url,
    }


def recover_wayback_metadata(
    conn: sqlite3.Connection,
    video_id: str,
    finder_data: dict[str, Any],
    *,
    timeout: float = 20.0,
    fetcher: Callable[[str], str] | None = None,
) -> bool:
    source_url = wayback_watch_url(finder_data)
    if source_url is None:
        return False

    if fetcher is None:
        fetcher = lambda url: fetch_wayback_page(url, timeout=timeout)

    try:
        html = fetcher(source_url)
    except Exception as exc:
        store_observation(
            conn,
            video_id,
            WAYBACK_SOURCE,
            "error",
            {"error": str(exc)},
            source_url=source_url,
        )
        return False

    normalised = normalise_wayback(video_id, html, source_url)
    if normalised is None:
        store_observation(
            conn,
            video_id,
            WAYBACK_SOURCE,
            "not_found",
            {"source_url": source_url},
            source_url=source_url,
        )
        return False

    store_observation(
        conn,
        video_id,
        WAYBACK_SOURCE,
        "found",
        normalised,
        source_url=source_url,
    )
    return True
