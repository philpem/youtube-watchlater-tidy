from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from .browser_session import DEFAULT_CDP_ENDPOINT, PlaywrightBrowserSession
from .watchlater_removal import (
    checkpoint_removal,
    finish_removal_run,
    pending_removal_items,
    removal_item_is_authorized,
    removal_plan_payload,
)

WATCH_LATER_URL = "https://www.youtube.com/playlist?list=WL"
DEFAULT_BROWSER_PROFILE = Path(".watchlater-playwright-profile")


@dataclass(frozen=True)
class BrowserRemovalAttempt:
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class BrowserScanEvent:
    kind: str
    video_id: str | None = None
    remaining_video_ids: tuple[str, ...] = ()
    complete: bool = False


@dataclass(frozen=True)
class _ScrollMetrics:
    top: int
    viewport: int
    height: int

    @property
    def at_bottom(self) -> bool:
        return self.top + self.viewport >= self.height - 4


@dataclass(frozen=True)
class BrowserRemovalResult:
    run_id: int
    applied: bool
    removed: int
    already_absent: int
    not_found: int
    failed: int
    stale: int
    remaining: int
    destructive_actions: int
    run_status: str


class WatchLaterBrowserClient(Protocol):
    def remove_video(self, video_id: str) -> BrowserRemovalAttempt: ...
    def close(self) -> None: ...


def _video_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    try:
        values = parse_qs(urlparse(href).query).get("v")
    except Exception:
        return None
    if not values:
        return None
    return values[0] or None


class PlaywrightWatchLaterClient:
    """Authenticated Watch Later UI adapter using a dedicated persistent profile."""

    def __init__(
        self,
        *,
        user_data_dir: str | Path = DEFAULT_BROWSER_PROFILE,
        headless: bool = False,
        channel: str | None = None,
        cdp_endpoint: str | None = None,
        action_menu_label: str = "Action menu",
        remove_label: str = "Remove from Watch later",
        max_scrolls: int = 250,
        scroll_pause: float = 0.7,
        stable_rounds: int = 4,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if max_scrolls < 1:
            raise ValueError("max_scrolls must be at least 1")
        if scroll_pause < 0:
            raise ValueError("scroll_pause cannot be negative")
        if stable_rounds < 1:
            raise ValueError("stable_rounds must be at least 1")
        self._session = PlaywrightBrowserSession(
            user_data_dir=user_data_dir,
            headless=headless,
            channel=channel,
            cdp_endpoint=cdp_endpoint,
        )
        self._context = self._session.context
        self._page = self._session.page
        self.action_menu_label = action_menu_label
        self.remove_label = remove_label
        self.max_scrolls = max_scrolls
        self.scroll_pause = scroll_pause
        self.stable_rounds = stable_rounds
        self.progress = progress
        self._loaded = False

    def close(self) -> None:
        self._session.close()

    def _ensure_watch_later(self) -> None:
        if self._loaded and "list=WL" in self._page.url:
            return
        self._page.goto(WATCH_LATER_URL, wait_until="domcontentloaded")
        self._page.wait_for_timeout(700)
        self._loaded = True

    def _matching_row(self, video_id: str):
        candidate = self._page.locator(
            f'ytd-playlist-video-renderer:has(a[href*="v={video_id}"])'
        )
        for index in range(candidate.count()):
            row = candidate.nth(index)
            anchors = row.locator('a[href*="watch"]')
            for anchor_index in range(anchors.count()):
                if _video_id_from_href(anchors.nth(anchor_index).get_attribute("href")) == video_id:
                    return row
        return None

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def _navigate_watch_later_start(self) -> None:
        self._page.goto(WATCH_LATER_URL, wait_until="domcontentloaded")
        self._page.wait_for_timeout(700)
        self._page.evaluate("window.scrollTo(0, 0)")
        self._loaded = True

    def _loaded_video_ids(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        hrefs = self._page.locator("ytd-playlist-video-renderer").evaluate_all(
            """rows => rows.map((row) => {
                const anchor = row.querySelector('a[href*="watch"]');
                return anchor ? anchor.getAttribute("href") : null;
            })"""
        )
        for href in hrefs:
            video_id = _video_id_from_href(href)
            if video_id and video_id not in seen:
                seen.add(video_id)
                result.append(video_id)
        return result

    def _scroll_metrics(self) -> _ScrollMetrics:
        value = self._page.evaluate(
            """() => {
                const body = document.body;
                const root = document.documentElement;
                const top = window.scrollY || root.scrollTop || (body && body.scrollTop) || 0;
                const height = Math.max(
                    body ? body.scrollHeight : 0,
                    root ? root.scrollHeight : 0
                );
                return {top: top, viewport: window.innerHeight, height: height};
            }"""
        )
        return _ScrollMetrics(
            top=int(value["top"]),
            viewport=int(value["viewport"]),
            height=int(value["height"]),
        )

    def _scroll_to_bottom(self) -> None:
        self._page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        if self.scroll_pause:
            self._page.wait_for_timeout(int(self.scroll_pause * 1000))

    def scan_matching_videos(self, video_ids: set[str]) -> Iterator[BrowserScanEvent]:
        """Yield matching loaded rows during one top-to-bottom Watch Later scan."""

        pending = set(video_ids)
        self._navigate_watch_later_start()
        self._emit(f"Scanning Watch Later for {len(pending)} planned video(s)")
        stable = 0
        last_content: tuple[int, tuple[str, ...]] | None = None

        for scroll_index in range(self.max_scrolls):
            if not pending:
                yield BrowserScanEvent("finished", complete=True)
                return

            loaded_ids = self._loaded_video_ids()
            matches = [video_id for video_id in loaded_ids if video_id in pending]
            if matches:
                self._emit(
                    f"Found {len(matches)} planned video(s) in the loaded rows; "
                    f"{len(pending)} remain in this scan"
                )
                for video_id in matches:
                    pending.remove(video_id)
                    yield BrowserScanEvent("candidate", video_id=video_id)
                stable = 0
                last_content = None
                if pending:
                    self._scroll_to_bottom()
                continue

            metrics = self._scroll_metrics()
            content = (len(loaded_ids), tuple(loaded_ids[-3:]))
            if metrics.at_bottom and content == last_content:
                stable += 1
            else:
                stable = 0

            if metrics.at_bottom:
                self._emit(
                    f"At the loaded end of Watch Later; waiting for more rows "
                    f"({stable}/{self.stable_rounds} stable checks)"
                )
            elif scroll_index == 0 or (scroll_index + 1) % 10 == 0:
                self._emit(
                    f"Loaded {len(loaded_ids)} Watch Later row(s); "
                    f"scroll {scroll_index + 1}/{self.max_scrolls}"
                )

            if stable >= self.stable_rounds:
                self._emit(
                    f"Completed one Watch Later scan; {len(pending)} planned video(s) absent"
                )
                yield BrowserScanEvent(
                    "finished",
                    remaining_video_ids=tuple(sorted(pending)),
                    complete=True,
                )
                return

            last_content = content
            self._scroll_to_bottom()

        self._emit(
            f"Stopped at the configured {self.max_scrolls}-scroll limit; "
            f"{len(pending)} planned video(s) were not resolved"
        )
        yield BrowserScanEvent(
            "finished",
            remaining_video_ids=tuple(sorted(pending)),
            complete=False,
        )

    def remove_loaded_video(self, video_id: str) -> BrowserRemovalAttempt:
        """Remove one exact ID which the current single-pass scan has already loaded."""

        row = self._matching_row(video_id)
        if row is None:
            return BrowserRemovalAttempt(
                "not_found",
                "exact row disappeared after it was matched in the loaded Watch Later rows",
            )

        return self._remove_row(video_id, row)

    def _remove_row(self, video_id: str, row) -> BrowserRemovalAttempt:
        row.scroll_into_view_if_needed()
        title_anchor = row.locator('a[href*="watch"]').first
        actual = _video_id_from_href(title_anchor.get_attribute("href"))
        if actual != video_id:
            raise RuntimeError(
                f"row identity changed before menu click: expected {video_id!r}, found {actual!r}"
            )

        menu = row.locator(f'button[aria-label="{self.action_menu_label}"]')
        if menu.count() == 0:
            menu = row.locator("#menu button")
        if menu.count() == 0:
            raise RuntimeError(
                f"could not find action menu for exact Watch Later row {video_id}; "
                "YouTube DOM or localization may have changed"
            )
        menu.first.click()

        option = self._page.get_by_text(self.remove_label, exact=True)
        if option.count() == 0:
            option = self._page.locator("ytd-menu-service-item-renderer").filter(
                has_text=self.remove_label
            )
        if option.count() == 0:
            self._page.keyboard.press("Escape")
            raise RuntimeError(
                f"could not find menu item {self.remove_label!r}; use --remove-label for "
                "the current YouTube language/UI"
            )

        actual = _video_id_from_href(title_anchor.get_attribute("href"))
        if actual != video_id:
            self._page.keyboard.press("Escape")
            raise RuntimeError(
                f"row identity changed before removal: expected {video_id!r}, found {actual!r}"
            )
        option.first.click()

        try:
            row.wait_for(state="detached", timeout=5000)
        except Exception:
            if self._matching_row(video_id) is not None:
                raise RuntimeError(
                    f"remove command was clicked for {video_id}, but the exact row remained visible"
                )
        return BrowserRemovalAttempt("removed")

    def _find_row_by_scrolling(self, video_id: str):
        self._ensure_watch_later()
        stable = 0
        last_height = -1
        last_count = -1
        for _ in range(self.max_scrolls):
            row = self._matching_row(video_id)
            if row is not None:
                return row, True
            count = self._page.locator("ytd-playlist-video-renderer").count()
            height = int(
                self._page.evaluate(
                    "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                )
            )
            if height == last_height and count == last_count:
                stable += 1
            else:
                stable = 0
            if stable >= self.stable_rounds:
                return None, True
            last_height, last_count = height, count
            self._page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            if self.scroll_pause:
                self._page.wait_for_timeout(int(self.scroll_pause * 1000))
        return None, False

    def remove_video(self, video_id: str) -> BrowserRemovalAttempt:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
            raise ValueError(f"invalid YouTube video id {video_id!r}")
        row, complete_scan = self._find_row_by_scrolling(video_id)
        if row is None:
            if complete_scan:
                return BrowserRemovalAttempt(
                    "already_absent",
                    "exact video ID was not present after a stable full Watch Later scan",
                )
            return BrowserRemovalAttempt(
                "not_found",
                "exact video ID was not found before the configured scroll limit",
            )
        return self._remove_row(video_id, row)


def open_login_session(
    *,
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
) -> None:
    """Open Watch Later in an already-running, manually launched Chromium browser.

    Authentication happens in the user's normal browser process rather than in a browser
    launched by Playwright. Start Chrome/Chromium with a remote-debugging endpoint first,
    sign in manually there, then use this helper only to verify/navigate the session.
    """
    session = PlaywrightBrowserSession(
        user_data_dir=DEFAULT_BROWSER_PROFILE,
        cdp_endpoint=cdp_endpoint,
    )
    try:
        session.page.goto(WATCH_LATER_URL, wait_until="domcontentloaded")
        input(
            "Use the attached browser to sign in to YouTube if needed, "
            "then press Enter here to disconnect: "
        )
    finally:
        session.close()


def execute_removal_plan(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    client: WatchLaterBrowserClient | None = None,
    apply: bool = False,
    confirmed: bool = False,
    max_deletes: int | None = None,
    interval: float = 2.0,
    retries: int = 1,
    backoff: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> BrowserRemovalResult:
    if max_deletes is not None and max_deletes < 0:
        raise ValueError("--max-deletes cannot be negative")
    if interval < 0:
        raise ValueError("--interval cannot be negative")
    if retries < 0:
        raise ValueError("--retries cannot be negative")
    if backoff < 0:
        raise ValueError("--backoff cannot be negative")

    payload = removal_plan_payload(conn, run_id)
    stale = int(payload["stale_item_count"])
    if apply and not confirmed:
        raise ValueError("destructive Watch Later removal requires both --apply and --confirm-remove")
    if apply and stale:
        raise ValueError(
            f"Watch Later removal plan {run_id} contains {stale} stale item(s); create a fresh plan"
        )

    run, pending = pending_removal_items(conn, run_id)
    if not apply:
        return BrowserRemovalResult(
            run_id=run_id,
            applied=False,
            removed=0,
            already_absent=0,
            not_found=0,
            failed=0,
            stale=stale,
            remaining=len(pending),
            destructive_actions=0,
            run_status=str(run["status"]),
        )
    if client is None:
        raise ValueError("a browser client is required with --apply")

    with conn:
        conn.execute("UPDATE watchlater_removal_runs SET status = 'running' WHERE id = ?", (run_id,))

    removed = absent = not_found = failed = stale_runtime = destructive = 0

    def emit(message: str) -> None:
        if progress is not None:
            progress(message)

    def process_candidate(item: sqlite3.Row, remover: Callable[[str], BrowserRemovalAttempt]) -> None:
        nonlocal removed, absent, not_found, failed, stale_runtime, destructive
        if not removal_item_is_authorized(conn, run, item):
            stale_runtime += 1
            emit(f"Skipping stale removal decision for {item['video_id']}")
            return

        video_id = str(item["video_id"])
        emit(f"Removing exact Watch Later video {video_id}")
        final: BrowserRemovalAttempt | None = None
        last_error: Exception | None = None
        for attempt_index in range(retries + 1):
            if not removal_item_is_authorized(conn, run, item):
                stale_runtime += 1
                final = None
                break
            try:
                candidate = remover(video_id)
                if candidate.status not in {"removed", "already_absent", "not_found"}:
                    raise RuntimeError(f"browser client returned invalid status {candidate.status!r}")
                final = candidate
                if candidate.status != "not_found" or attempt_index >= retries:
                    break
            except Exception as exc:
                last_error = exc
                if attempt_index >= retries:
                    break
            if backoff:
                sleeper(backoff * (2**attempt_index))

        if final is None and last_error is None:
            return
        if final is None:
            failed += 1
            checkpoint_removal(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="failed",
                error=str(last_error),
            )
            emit(f"Failed {video_id}: {last_error}")
            return

        if final.status == "removed":
            removed += 1
            destructive += 1
            checkpoint_removal(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="removed",
                error=final.detail,
            )
            emit(f"Removed {video_id}; checkpoint saved")
            if interval:
                sleeper(interval)
        elif final.status == "already_absent":
            absent += 1
            checkpoint_removal(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="already_absent",
                error=final.detail,
            )
            emit(f"Already absent {video_id}; checkpoint saved")
        else:
            not_found += 1
            checkpoint_removal(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="not_found",
                error=final.detail,
            )
            emit(f"Not found {video_id}; left retriable")

    scan_matching = getattr(client, "scan_matching_videos", None)
    remove_loaded = getattr(client, "remove_loaded_video", None)
    if callable(scan_matching) and callable(remove_loaded):
        items_by_video = {str(item["video_id"]): item for item in pending}
        for event in scan_matching(set(items_by_video)):
            if event.kind == "candidate":
                if max_deletes is not None and destructive >= max_deletes:
                    emit(f"Reached --max-deletes {max_deletes}; remaining items stay resumable")
                    break
                if event.video_id is None or event.video_id not in items_by_video:
                    raise RuntimeError("browser scan returned an unknown removal candidate")
                process_candidate(items_by_video[event.video_id], remove_loaded)
                continue

            if event.kind != "finished":
                raise RuntimeError(f"browser scan returned invalid event {event.kind!r}")
            terminal_status = "already_absent" if event.complete else "not_found"
            detail = (
                "exact video ID was not present after one stable full Watch Later scan"
                if event.complete
                else "exact video ID was not resolved before the configured scroll limit"
            )
            for video_id in event.remaining_video_ids:
                item = items_by_video.get(video_id)
                if item is None:
                    raise RuntimeError("browser scan returned an unknown unresolved video ID")
                if not removal_item_is_authorized(conn, run, item):
                    stale_runtime += 1
                    continue
                checkpoint_removal(
                    conn,
                    run_id=run_id,
                    ordinal=int(item["ordinal"]),
                    status=terminal_status,
                    error=detail,
                )
                if event.complete:
                    absent += 1
                else:
                    not_found += 1
            break
    else:
        for item in pending:
            if max_deletes is not None and destructive >= max_deletes:
                break
            process_candidate(item, client.remove_video)

    status, remaining = finish_removal_run(conn, run_id)
    return BrowserRemovalResult(
        run_id=run_id,
        applied=True,
        removed=removed,
        already_absent=absent,
        not_found=not_found,
        failed=failed,
        stale=stale_runtime,
        remaining=remaining,
        destructive_actions=destructive,
        run_status=status,
    )
