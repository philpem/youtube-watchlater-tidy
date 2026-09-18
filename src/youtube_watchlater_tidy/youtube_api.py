from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from tqdm import tqdm

from .multi_destination import current_move_authorizes_destination
from .multi_destination_support import plan_payload
from .progress import ProgressCallback, ProgressEvent
from .playlist_sync import (
    INVENTORY_FORMAT,
    ensure_playlist_sync_schema,
    import_inventory,
)

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
DEFAULT_CLIENT_SECRETS = Path("client_secret.json")
DEFAULT_TOKEN_FILE = Path(".youtube-watchlater-token.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class YouTubeApiClient(Protocol):
    def list_playlists(self) -> list[dict[str, Any]]: ...

    def list_playlist_items(self, playlist_id: str) -> list[dict[str, Any]]: ...

    def find_playlist_item(self, playlist_id: str, video_id: str) -> dict[str, Any] | None: ...

    def create_playlist(self, title: str, privacy_status: str) -> dict[str, Any]: ...

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> dict[str, Any]: ...


class GoogleYouTubeClient:
    """Thin adapter around google-api-python-client's YouTube v3 service."""

    def __init__(self, service: Any):
        self.service = service

    def list_playlists(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            response = (
                self.service.playlists()
                .list(part="snippet,status", mine=True, maxResults=50, pageToken=token)
                .execute()
            )
            result.extend(item for item in response.get("items", []) if isinstance(item, dict))
            token = response.get("nextPageToken")
            if not token:
                break
        return result

    def list_playlist_items(self, playlist_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            response = (
                self.service.playlistItems()
                .list(
                    part="snippet,status",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=token,
                )
                .execute()
            )
            result.extend(item for item in response.get("items", []) if isinstance(item, dict))
            token = response.get("nextPageToken")
            if not token:
                break
        return result

    def find_playlist_item(self, playlist_id: str, video_id: str) -> dict[str, Any] | None:
        response = (
            self.service.playlistItems()
            .list(
                part="snippet,status",
                playlistId=playlist_id,
                videoId=video_id,
                maxResults=50,
            )
            .execute()
        )
        items = response.get("items", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            resource = snippet.get("resourceId") or {}
            if resource.get("videoId") == video_id:
                return item
        return None

    def create_playlist(self, title: str, privacy_status: str) -> dict[str, Any]:
        return (
            self.service.playlists()
            .insert(
                part="snippet,status",
                body={
                    "snippet": {"title": title},
                    "status": {"privacyStatus": privacy_status},
                },
            )
            .execute()
        )

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> dict[str, Any]:
        return (
            self.service.playlistItems()
            .insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id,
                        },
                    }
                },
            )
            .execute()
        )


def authenticated_client(
    *,
    client_secrets: str | Path = DEFAULT_CLIENT_SECRETS,
    token_file: str | Path = DEFAULT_TOKEN_FILE,
) -> GoogleYouTubeClient:
    """Authorize an installed application and return a YouTube Data API client."""

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "YouTube API support is not installed; run `pip install -e '.[youtube-api]'`"
        ) from exc

    client_secrets = Path(client_secrets)
    token_file = Path(token_file)
    if not client_secrets.exists():
        raise ValueError(f"OAuth client secrets file does not exist: {client_secrets}")

    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), [YOUTUBE_SCOPE])

    if credentials is None or not credentials.valid:
        if credentials is not None and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets),
                scopes=[YOUTUBE_SCOPE],
            )
            credentials = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json() + "\n", encoding="utf-8")
        try:
            os.chmod(token_file, 0o600)
        except OSError:
            pass

    service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    return GoogleYouTubeClient(service)


def _playlist_row(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    playlist_id = item.get("id")
    title = snippet.get("title")
    if not isinstance(playlist_id, str) or not playlist_id:
        raise ValueError("YouTube API returned a playlist without an id")
    if not isinstance(title, str) or not title:
        raise ValueError(f"YouTube API returned playlist {playlist_id!r} without a title")
    return {
        "playlist_id": playlist_id,
        "title": title,
        "privacy_status": status.get("privacyStatus") or "unknown",
        "items": [],
        "api_raw": item,
    }


def _playlist_item_row(item: dict[str, Any]) -> dict[str, Any] | None:
    snippet = item.get("snippet") or {}
    resource = snippet.get("resourceId") or {}
    video_id = resource.get("videoId")
    if not isinstance(video_id, str) or not video_id:
        return None
    return {
        "video_id": video_id,
        "playlist_item_id": item.get("id") if isinstance(item.get("id"), str) else None,
        "position": snippet.get("position") if isinstance(snippet.get("position"), int) else None,
        "api_raw": item,
    }


def fetch_inventory_from_api(
    client: YouTubeApiClient,
    *,
    show_progress: bool = True,
) -> dict[str, Any]:
    playlists = [_playlist_row(item) for item in client.list_playlists()]
    progress = tqdm(
        playlists,
        desc="Playlist inventory",
        unit="playlist",
        disable=not show_progress,
        dynamic_ncols=True,
    )
    for playlist in progress:
        items = []
        for raw in client.list_playlist_items(playlist["playlist_id"]):
            row = _playlist_item_row(raw)
            if row is not None:
                items.append(row)
        playlist["items"] = items
        progress.set_postfix_str(playlist["title"][:50])
    return {
        "format": INVENTORY_FORMAT,
        "source": "youtube-data-api",
        "fetched_at": _utc_now(),
        "playlists": playlists,
    }


@dataclass(frozen=True)
class ApiExecutionResult:
    run_id: int
    applied: bool
    created_playlists: int
    inserted: int
    already_present: int
    failed: int
    stale: int
    remaining: int
    writes: int
    run_status: str


def _current_decision_matches(conn: sqlite3.Connection, run: sqlite3.Row, item: sqlite3.Row) -> bool:
    return current_move_authorizes_destination(
        conn,
        snapshot_id=int(run["snapshot_id"]),
        video_id=str(item["video_id"]),
        decision_event_id=int(item["decision_event_id"]),
        destination_playlist=str(item["destination_name"]),
    )


def _live_playlists_by_id_and_title(
    client: YouTubeApiClient,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows = [_playlist_row(item) for item in client.list_playlists()]
    by_id = {str(row["playlist_id"]): row for row in rows}
    by_title: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_title.setdefault(str(row["title"]).casefold(), []).append(row)
    return by_id, by_title


def _checkpoint_destination(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    name: str,
    playlist_id: str,
    status: str,
    raw: dict[str, Any] | None = None,
) -> None:
    now = _utc_now()
    privacy = "unknown"
    if raw:
        privacy = str((raw.get("status") or {}).get("privacyStatus") or "unknown")
    with conn:
        conn.execute(
            """
            UPDATE playlist_sync_destinations
            SET destination_playlist_id = ?, status = ?,
                privacy_status = CASE WHEN ? = 'unknown' THEN privacy_status ELSE ? END,
                created_at = CASE WHEN ? = 'created' THEN ? ELSE created_at END,
                error = NULL
            WHERE run_id = ? AND destination_name = ?
            """,
            (playlist_id, status, privacy, privacy, status, now, run_id, name),
        )
        if raw is not None:
            snippet = raw.get("snippet") or {}
            title = str(snippet.get("title") or name)
            conn.execute(
                """
                INSERT INTO youtube_playlist_inventory (
                    playlist_id, title, privacy_status, fetched_at, source, raw_json
                ) VALUES (?, ?, ?, ?, 'youtube-data-api-live', ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    title = excluded.title,
                    privacy_status = excluded.privacy_status,
                    fetched_at = excluded.fetched_at,
                    source = excluded.source,
                    raw_json = excluded.raw_json
                """,
                (playlist_id, title, privacy, now, json.dumps(raw, ensure_ascii=False, sort_keys=True)),
            )


def _checkpoint_item(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    ordinal: int,
    status: str,
    playlist_id: str | None,
    playlist_item_id: str | None = None,
    error: str | None = None,
    raw: dict[str, Any] | None = None,
    video_id: str | None = None,
) -> None:
    now = _utc_now()
    with conn:
        conn.execute(
            """
            UPDATE playlist_sync_items
            SET status = ?, destination_playlist_id = ?, playlist_item_id = ?,
                attempted_at = ?,
                completed_at = CASE WHEN ? IN ('inserted', 'already_present') THEN ? ELSE completed_at END,
                error = ?
            WHERE run_id = ? AND ordinal = ?
            """,
            (
                status,
                playlist_id,
                playlist_item_id,
                now,
                status,
                now,
                error,
                run_id,
                ordinal,
            ),
        )
        if status in {"inserted", "already_present"} and playlist_id and video_id:
            position = None
            if raw:
                position = (raw.get("snippet") or {}).get("position")
                if not isinstance(position, int):
                    position = None
            conn.execute(
                """
                INSERT INTO youtube_playlist_inventory_items (
                    playlist_id, video_id, playlist_item_id, position, raw_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                    playlist_item_id = excluded.playlist_item_id,
                    position = excluded.position,
                    raw_json = excluded.raw_json
                """,
                (
                    playlist_id,
                    video_id,
                    playlist_item_id,
                    position,
                    json.dumps(raw or {}, ensure_ascii=False, sort_keys=True),
                ),
            )


def _finish_run(conn: sqlite3.Connection, run_id: int) -> tuple[str, int]:
    remaining = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM playlist_sync_items
            WHERE run_id = ?
              AND (
                    status IN ('planned', 'failed', 'skipped')
                    OR (status = 'already_present' AND attempted_at IS NULL)
              )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    status = "complete" if remaining == 0 else "partial"
    with conn:
        conn.execute("UPDATE playlist_sync_runs SET status = ? WHERE id = ?", (status, run_id))
    return status, remaining


def execute_api_plan(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    client: YouTubeApiClient | None = None,
    apply: bool = False,
    allow_over_quota: bool = False,
    max_writes: int | None = None,
    progress: ProgressCallback | None = None,
    phase: str = "Playlist sync (API)",
) -> ApiExecutionResult:
    ensure_playlist_sync_schema(conn)
    if max_writes is not None and max_writes < 0:
        raise ValueError("--max-writes cannot be negative")
    run = conn.execute("SELECT * FROM playlist_sync_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"playlist sync plan {run_id} does not exist")
    if run["backend"] != "api":
        raise ValueError(f"playlist sync plan {run_id} uses backend {run['backend']!r}, not 'api'")

    payload = plan_payload(conn, run_id)
    stale = int(payload["stale_item_count"])
    if apply and stale:
        raise ValueError(
            f"playlist sync plan {run_id} contains {stale} stale item(s); create a fresh plan before applying"
        )
    if apply and bool(run["exceeds_quota"]) and not allow_over_quota:
        raise ValueError(
            f"playlist sync plan {run_id} estimates {run['estimated_quota']} quota units, "
            f"exceeding configured limit {run['quota_limit']}"
        )

    existing_counts = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM playlist_sync_items WHERE run_id = ? GROUP BY status",
            (run_id,),
        )
    }
    if not apply:
        remaining = existing_counts.get("planned", 0) + existing_counts.get("failed", 0) + existing_counts.get("skipped", 0)
        return ApiExecutionResult(
            run_id=run_id,
            applied=False,
            created_playlists=0,
            inserted=0,
            already_present=existing_counts.get("already_present", 0),
            failed=existing_counts.get("failed", 0),
            stale=stale,
            remaining=remaining,
            writes=0,
            run_status=str(run["status"]),
        )

    if client is None:
        raise ValueError("an authenticated YouTube API client is required with --apply")

    if progress is not None:
        progress(ProgressEvent(phase=phase, kind="status", detail="loading live playlists"))
    by_id, by_title = _live_playlists_by_id_and_title(client)
    writes = created = inserted = live_present = failed = stale_runtime = 0
    stopped_for_cap = False
    with conn:
        conn.execute("UPDATE playlist_sync_runs SET status = 'running' WHERE id = ?", (run_id,))

    def can_write() -> bool:
        return max_writes is None or writes < max_writes

    items = conn.execute(
        """
        SELECT * FROM playlist_sync_items
        WHERE run_id = ?
          AND (
                status IN ('planned', 'failed', 'skipped')
                OR (status = 'already_present' AND attempted_at IS NULL)
          )
        ORDER BY ordinal
        """,
        (run_id,),
    ).fetchall()

    completed_items = 0

    def mark_processed(detail: str | None = None) -> None:
        nonlocal completed_items
        completed_items += 1
        if progress is not None:
            progress(
                ProgressEvent(
                    phase=phase,
                    kind="update",
                    completed=completed_items,
                    total=len(items),
                    unit="item",
                    detail=detail,
                    counters={
                        "created": created,
                        "inserted": inserted,
                        "present": live_present,
                        "failed": failed,
                        "stale": stale_runtime,
                    },
                )
            )

    if progress is not None:
        progress(
            ProgressEvent(
                phase=phase,
                kind="start",
                completed=0,
                total=len(items),
                unit="item",
                detail=f"run {run_id}",
            )
        )

    for item in items:
        if not _current_decision_matches(conn, run, item):
            stale_runtime += 1
            mark_processed("stale decision")
            continue

        destination = conn.execute(
            """
            SELECT * FROM playlist_sync_destinations
            WHERE run_id = ? AND destination_name = ?
            """,
            (run_id, item["destination_name"]),
        ).fetchone()
        assert destination is not None
        playlist_id = destination["destination_playlist_id"]

        try:
            if playlist_id:
                playlist_id = str(playlist_id)
                if playlist_id not in by_id:
                    raise RuntimeError(
                        f"planned destination {item['destination_name']!r} had playlist id {playlist_id!r}, "
                        "but that id is no longer present in the authenticated account; refresh inventory and re-plan"
                    )
            else:
                matches = by_title.get(str(item["destination_name"]).casefold(), [])
                if len(matches) > 1:
                    raise RuntimeError(
                        f"destination title {item['destination_name']!r} is ambiguous in the live account"
                    )
                if len(matches) == 1:
                    live = matches[0]
                    playlist_id = str(live["playlist_id"])
                    _checkpoint_destination(
                        conn,
                        run_id=run_id,
                        name=str(item["destination_name"]),
                        playlist_id=playlist_id,
                        status="existing",
                        raw=live.get("api_raw"),
                    )
                else:
                    if not can_write():
                        stopped_for_cap = True
                        break
                    raw_created = client.create_playlist(
                        str(item["destination_name"]),
                        str(destination["privacy_status"]),
                    )
                    writes += 1
                    created += 1
                    live = _playlist_row(raw_created)
                    playlist_id = str(live["playlist_id"])
                    by_id[playlist_id] = live
                    by_title.setdefault(str(live["title"]).casefold(), []).append(live)
                    _checkpoint_destination(
                        conn,
                        run_id=run_id,
                        name=str(item["destination_name"]),
                        playlist_id=playlist_id,
                        status="created",
                        raw=raw_created,
                    )

            if not _current_decision_matches(conn, run, item):
                stale_runtime += 1
                mark_processed("stale decision")
                continue

            existing = client.find_playlist_item(playlist_id, str(item["video_id"]))
            if existing is not None:
                live_present += 1
                _checkpoint_item(
                    conn,
                    run_id=run_id,
                    ordinal=int(item["ordinal"]),
                    status="already_present",
                    playlist_id=playlist_id,
                    playlist_item_id=existing.get("id") if isinstance(existing.get("id"), str) else None,
                    raw=existing,
                    video_id=str(item["video_id"]),
                )
                mark_processed("already present")
                continue

            if not can_write():
                stopped_for_cap = True
                break
            raw_inserted = client.insert_playlist_item(playlist_id, str(item["video_id"]))
            writes += 1
            inserted += 1
            _checkpoint_item(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="inserted",
                playlist_id=playlist_id,
                playlist_item_id=raw_inserted.get("id") if isinstance(raw_inserted.get("id"), str) else None,
                raw=raw_inserted,
                video_id=str(item["video_id"]),
            )
        except Exception as exc:
            failed += 1
            _checkpoint_item(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="failed",
                playlist_id=str(playlist_id) if playlist_id else None,
                error=str(exc),
                video_id=str(item["video_id"]),
            )
        mark_processed(str(item["video_id"]))

    status, remaining = _finish_run(conn, run_id)
    if stopped_for_cap and status == "complete":
        status = "partial"
        with conn:
            conn.execute("UPDATE playlist_sync_runs SET status = 'partial' WHERE id = ?", (run_id,))
    if progress is not None:
        progress(
            ProgressEvent(
                phase=phase,
                kind="finish",
                completed=completed_items,
                total=len(items),
                unit="item",
                detail=f"status={status} remaining={remaining}",
            )
        )
    return ApiExecutionResult(
        run_id=run_id,
        applied=True,
        created_playlists=created,
        inserted=inserted,
        already_present=live_present,
        failed=failed,
        stale=stale_runtime,
        remaining=remaining,
        writes=writes,
        run_status=status,
    )


def refresh_inventory(
    conn: sqlite3.Connection,
    client: YouTubeApiClient,
    *,
    show_progress: bool = True,
):
    payload = fetch_inventory_from_api(client, show_progress=show_progress)
    return import_inventory(conn, payload)
