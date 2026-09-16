from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

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
        action_menu_label: str = "Action menu",
        remove_label: str = "Remove from Watch later",
        max_scrolls: int = 250,
        scroll_pause: float = 0.7,
        stable_rounds: int = 4,
    ) -> None:
        if max_scrolls < 1:
            raise ValueError("max_scrolls must be at least 1")
        if scroll_pause < 0:
            raise ValueError("scroll_pause cannot be negative")
        if stable_rounds < 1:
            raise ValueError("stable_rounds must be at least 1")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "browser execution support is not installed; run `pip install -e '.[browser]'` "
                "and `playwright install chromium`"
            ) from exc

        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {
            "user_data_dir": str(Path(user_data_dir)),
            "headless": headless,
        }
        if channel:
            launch["channel"] = channel
        self._context = self._pw.chromium.launch_persistent_context(**launch)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.action_menu_label = action_menu_label
        self.remove_label = remove_label
        self.max_scrolls = max_scrolls
        self.scroll_pause = scroll_pause
        self.stable_rounds = stable_rounds
        self._loaded = False

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._pw.stop()

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

        # Re-check exact identity immediately before the destructive click.
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


def open_login_session(
    *,
    user_data_dir: str | Path = DEFAULT_BROWSER_PROFILE,
    channel: str | None = None,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "browser execution support is not installed; run `pip install -e '.[browser]'` "
            "and `playwright install chromium`"
        ) from exc
    with sync_playwright() as pw:
        kwargs: dict[str, Any] = {
            "user_data_dir": str(Path(user_data_dir)),
            "headless": False,
        }
        if channel:
            kwargs["channel"] = channel
        context = pw.chromium.launch_persistent_context(**kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(WATCH_LATER_URL, wait_until="domcontentloaded")
        input("Sign in to YouTube in the opened browser if needed, then press Enter here to close it: ")
        context.close()


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
    for item in pending:
        if max_deletes is not None and destructive >= max_deletes:
            break
        if not removal_item_is_authorized(conn, run, item):
            stale_runtime += 1
            continue

        final: BrowserRemovalAttempt | None = None
        last_error: Exception | None = None
        for attempt_index in range(retries + 1):
            if not removal_item_is_authorized(conn, run, item):
                stale_runtime += 1
                final = None
                break
            try:
                candidate = client.remove_video(str(item["video_id"]))
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
            continue
        if final is None:
            failed += 1
            checkpoint_removal(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="failed",
                error=str(last_error),
            )
            continue

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
        else:
            not_found += 1
            checkpoint_removal(
                conn,
                run_id=run_id,
                ordinal=int(item["ordinal"]),
                status="not_found",
                error=final.detail,
            )

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
