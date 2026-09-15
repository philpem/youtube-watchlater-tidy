from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen

from tqdm import tqdm

from .enrichment import _run_yt_dlp
from .llm_store import latest_run_id
from .reports import latest_snapshot_id

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}

TRANSCRIPT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS transcript_observations (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    request_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('found', 'not_found', 'error')),
    source_type TEXT CHECK(source_type IN ('manual', 'automatic')),
    language TEXT,
    language_name TEXT,
    format TEXT,
    transcript_text TEXT,
    segments_json TEXT NOT NULL,
    source_url TEXT,
    metadata_json TEXT NOT NULL,
    raw_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcript_observations_video_request
    ON transcript_observations(video_id, request_key, id);
"""


@dataclass(frozen=True)
class TranscriptResult:
    attempted: int
    found: int
    manual: int
    automatic: int
    not_found: int
    failed: int


@dataclass(frozen=True)
class CaptionTrack:
    source_type: str
    language: str
    language_name: str | None
    ext: str
    url: str | None
    data: str | None
    http_headers: dict[str, str]


@dataclass(frozen=True)
class ParsedTranscript:
    text: str
    segments: tuple[dict[str, Any], ...]
    raw_text: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_transcript_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(TRANSCRIPT_SCHEMA_SQL)


def transcript_request_key(languages: list[str], allow_automatic: bool) -> str:
    normalized = [item.strip().casefold() for item in languages if item.strip()]
    if not normalized:
        raise ValueError("at least one transcript language must be supplied")
    payload = {
        "version": 1,
        "languages": normalized,
        "allow_automatic": bool(allow_automatic),
        "format_preference": ["json3", "vtt"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _latest_cached_ids(conn: sqlite3.Connection, request_key: str) -> set[str]:
    ensure_transcript_schema(conn)
    rows = conn.execute(
        """
        SELECT t.video_id
        FROM transcript_observations AS t
        WHERE t.request_key = ?
          AND t.status IN ('found', 'not_found')
          AND NOT EXISTS (
              SELECT 1 FROM transcript_observations AS newer
              WHERE newer.video_id = t.video_id
                AND newer.request_key = t.request_key
                AND newer.status IN ('found', 'not_found')
                AND newer.id > t.id
          )
        """,
        (request_key,),
    ).fetchall()
    return {str(row["video_id"]) for row in rows}


def candidate_video_ids(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    all_videos: bool = False,
    video_ids: list[str] | None = None,
    selection_id: int | None = None,
    llm_needs_transcript: bool = False,
    run_id: int | None = None,
    remaining: bool = True,
    languages: list[str] | None = None,
    allow_automatic: bool = True,
    limit: int | None = None,
    refresh: bool = False,
) -> list[str]:
    ensure_transcript_schema(conn)
    languages = languages or ["en"]
    request_key = transcript_request_key(languages, allow_automatic)
    explicit = list(dict.fromkeys(video_ids or ()))
    target_count = sum(
        (
            int(all_videos),
            int(bool(explicit)),
            int(selection_id is not None),
            int(llm_needs_transcript),
        )
    )
    if target_count != 1:
        raise ValueError(
            "choose exactly one transcript target: --all, --video-id, --selection, "
            "or --llm-needs-transcript"
        )
    if run_id is not None and not llm_needs_transcript:
        raise ValueError("--run-id is only valid with --llm-needs-transcript")
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
    elif llm_needs_transcript:
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
                WHERE run_id = ? AND needs_transcript = 1
                """,
                (run_id,),
            )
        }
    elif snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    assert snapshot_id is not None
    rows = conn.execute(
        """
        SELECT e.position, e.video_id, e.title, d.action AS current_action
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

    cached = set() if refresh else _latest_cached_ids(conn, request_key)
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
        if video_id in cached:
            continue
        result.append(video_id)
        if limit is not None and len(result) >= limit:
            break
    return result


def _language_score(language: str, name: str | None, preferences: list[str]) -> int | None:
    language_cf = language.casefold()
    name_cf = (name or "").casefold()
    best: int | None = None
    for index, preference in enumerate(preferences):
        pref = preference.casefold().strip()
        if not pref:
            continue
        score: int | None = None
        if language_cf == pref:
            score = 10000 - index
        elif language_cf.startswith(pref + "-"):
            score = 9000 - index
        elif language_cf.split("-", 1)[0] == pref.split("-", 1)[0]:
            score = 8000 - index
        elif pref in name_cf:
            score = 7000 - index
        if score is not None and (best is None or score > best):
            best = score
    return best


def _format_choice(formats: Any) -> tuple[str, str | None, str | None, dict[str, str]] | None:
    if not isinstance(formats, list):
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    preference = {"json3": 2, "vtt": 1}
    for entry in formats:
        if not isinstance(entry, dict):
            continue
        ext = str(entry.get("ext") or "").casefold()
        if ext not in preference:
            continue
        if not isinstance(entry.get("url"), str) and not isinstance(entry.get("data"), str):
            continue
        candidates.append((preference[ext], entry))
    if not candidates:
        return None
    _, entry = max(candidates, key=lambda item: item[0])
    headers = entry.get("http_headers")
    if not isinstance(headers, dict):
        headers = {}
    return (
        str(entry.get("ext")).casefold(),
        entry.get("url") if isinstance(entry.get("url"), str) else None,
        entry.get("data") if isinstance(entry.get("data"), str) else None,
        {str(key): str(value) for key, value in headers.items()},
    )


def select_caption_track(
    metadata: dict[str, Any],
    *,
    languages: list[str],
    allow_automatic: bool,
) -> CaptionTrack | None:
    sources: list[tuple[str, Any]] = [("manual", metadata.get("subtitles"))]
    if allow_automatic:
        sources.append(("automatic", metadata.get("automatic_captions")))

    for source_type, table in sources:
        if not isinstance(table, dict):
            continue
        ranked: list[tuple[int, str, str | None, tuple[str, str | None, str | None, dict[str, str]]]] = []
        for language, formats in table.items():
            if not isinstance(language, str):
                continue
            name = None
            if isinstance(formats, list):
                for entry in reversed(formats):
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                        name = entry["name"]
                        break
            score = _language_score(language, name, languages)
            choice = _format_choice(formats)
            if score is None or choice is None:
                continue
            ranked.append((score, language, name, choice))
        if ranked:
            _, language, name, choice = max(ranked, key=lambda item: item[0])
            ext, url, data, headers = choice
            return CaptionTrack(
                source_type=source_type,
                language=language,
                language_name=name,
                ext=ext,
                url=url,
                data=data,
                http_headers=headers,
            )
    return None


def _parse_json3(raw_text: str) -> ParsedTranscript:
    data = json.loads(raw_text)
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise ValueError("json3 caption payload has no events array")
    segments: list[dict[str, Any]] = []
    lines: list[str] = []
    previous = None
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("segs"), list):
            continue
        text = "".join(
            str(segment.get("utf8") or "")
            for segment in event["segs"]
            if isinstance(segment, dict)
        )
        text = re.sub(r"\s+", " ", text).strip()
        if not text or text == previous:
            continue
        previous = text
        segment = {
            "start_ms": event.get("tStartMs"),
            "duration_ms": event.get("dDurationMs"),
            "text": text,
        }
        segments.append(segment)
        lines.append(text)
    return ParsedTranscript(text="\n".join(lines), segments=tuple(segments), raw_text=raw_text)


def _parse_vtt(raw_text: str) -> ParsedTranscript:
    lines: list[str] = []
    previous = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        if line.isdigit() or line.startswith(("NOTE", "STYLE", "REGION", "Kind:", "Language:")):
            continue
        line = html.unescape(re.sub(r"<[^>]+>", "", line))
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == previous:
            continue
        previous = line
        lines.append(line)
    segments = tuple({"start_ms": None, "duration_ms": None, "text": line} for line in lines)
    return ParsedTranscript(text="\n".join(lines), segments=segments, raw_text=raw_text)


def parse_caption_payload(ext: str, raw_text: str) -> ParsedTranscript:
    if ext == "json3":
        return _parse_json3(raw_text)
    if ext == "vtt":
        return _parse_vtt(raw_text)
    raise ValueError(f"unsupported caption format {ext!r}")


def _fetch_payload(track: CaptionTrack, timeout: float = 30.0) -> str:
    if track.data is not None:
        return track.data
    if track.url is None:
        raise ValueError("caption track has neither inline data nor URL")
    headers = {
        "Accept": "*/*",
        "User-Agent": "youtube-watchlater-tidy/0.1 (+https://github.com/philpem/youtube-watchlater-tidy)",
        **track.http_headers,
    }
    with urlopen(Request(track.url, headers=headers), timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _store_observation(
    conn: sqlite3.Connection,
    video_id: str,
    request_key: str,
    status: str,
    *,
    track: CaptionTrack | None = None,
    parsed: ParsedTranscript | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    ensure_transcript_schema(conn)
    meta = {
        "available_subtitles": sorted((metadata or {}).get("subtitles", {}).keys())
        if isinstance((metadata or {}).get("subtitles"), dict)
        else [],
        "available_automatic_captions": sorted((metadata or {}).get("automatic_captions", {}).keys())
        if isinstance((metadata or {}).get("automatic_captions"), dict)
        else [],
    }
    if error is not None:
        meta["error"] = error
    with conn:
        conn.execute(
            """
            INSERT INTO transcript_observations (
                video_id, fetched_at, request_key, status,
                source_type, language, language_name, format,
                transcript_text, segments_json, source_url,
                metadata_json, raw_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                _utc_now(),
                request_key,
                status,
                track.source_type if track else None,
                track.language if track else None,
                track.language_name if track else None,
                track.ext if track else None,
                parsed.text if parsed else None,
                json.dumps(parsed.segments if parsed else (), ensure_ascii=False, separators=(",", ":")),
                track.url if track else None,
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                parsed.raw_text if parsed else "",
            ),
        )


def fetch_transcripts(
    conn: sqlite3.Connection,
    video_ids: list[str],
    *,
    languages: list[str] | None = None,
    allow_automatic: bool = True,
    yt_dlp: str = "yt-dlp",
    workers: int = 4,
    start_interval: float = 0.5,
    timeout: float = 30.0,
    metadata_fetcher: Callable[[str], dict[str, Any]] | None = None,
    payload_fetcher: Callable[[CaptionTrack], str] | None = None,
    show_progress: bool = True,
) -> TranscriptResult:
    ensure_transcript_schema(conn)
    languages = languages or ["en"]
    request_key = transcript_request_key(languages, allow_automatic)
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

    def fetch_one(video_id: str):
        wait_for_slot()
        try:
            metadata = (
                metadata_fetcher(video_id)
                if metadata_fetcher is not None
                else _run_yt_dlp(video_id, yt_dlp=yt_dlp)
            )
            track = select_caption_track(
                metadata,
                languages=languages,
                allow_automatic=allow_automatic,
            )
            if track is None:
                return video_id, metadata, None, None, None
            raw = payload_fetcher(track) if payload_fetcher is not None else _fetch_payload(track, timeout)
            parsed = parse_caption_payload(track.ext, raw)
            if not parsed.text.strip():
                return video_id, metadata, track, None, None
            return video_id, metadata, track, parsed, None
        except Exception as exc:
            return video_id, None, None, None, exc

    found = manual = automatic = not_found = failed = 0
    progress = tqdm(
        total=len(video_ids),
        desc="Transcripts",
        unit="video",
        disable=not show_progress,
        dynamic_ncols=True,
    )
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_one, video_id): video_id for video_id in video_ids}
            for future in as_completed(futures):
                video_id, metadata, track, parsed, error = future.result()
                if error is not None:
                    failed += 1
                    _store_observation(
                        conn,
                        video_id,
                        request_key,
                        "error",
                        error=str(error),
                    )
                elif track is None or parsed is None:
                    not_found += 1
                    _store_observation(
                        conn,
                        video_id,
                        request_key,
                        "not_found",
                        track=track,
                        metadata=metadata,
                    )
                else:
                    found += 1
                    if track.source_type == "manual":
                        manual += 1
                    else:
                        automatic += 1
                    _store_observation(
                        conn,
                        video_id,
                        request_key,
                        "found",
                        track=track,
                        parsed=parsed,
                        metadata=metadata,
                    )
                progress.update(1)
                progress.set_postfix_str(
                    f"{video_id} found={found} manual={manual} auto={automatic} missing={not_found} failed={failed}"
                )
    finally:
        progress.close()

    return TranscriptResult(
        attempted=len(video_ids),
        found=found,
        manual=manual,
        automatic=automatic,
        not_found=not_found,
        failed=failed,
    )


def latest_transcript(conn: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    ensure_transcript_schema(conn)
    return conn.execute(
        """
        SELECT * FROM transcript_observations
        WHERE video_id = ? AND status = 'found'
        ORDER BY id DESC LIMIT 1
        """,
        (video_id,),
    ).fetchone()
