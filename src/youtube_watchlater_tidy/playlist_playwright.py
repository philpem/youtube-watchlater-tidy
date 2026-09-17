from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .playlist_browser import BrowserPlaylist, BrowserPlaylistItem
from .watchlater_browser import DEFAULT_BROWSER_PROFILE

PLAYLISTS_URL = "https://www.youtube.com/feed/playlists"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
PLAYLIST_URL = "https://www.youtube.com/playlist?list={playlist_id}"


def _playlist_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    try:
        values = parse_qs(urlparse(href).query).get("list")
    except Exception:
        return None
    if not values:
        return None
    value = values[0]
    return value if value else None


def _video_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    try:
        values = parse_qs(urlparse(href).query).get("v")
    except Exception:
        return None
    if not values:
        return None
    value = values[0]
    return value if value else None


class PlaywrightPlaylistClient:
    """Conservative YouTube playlist UI adapter using a dedicated Playwright profile.

    YouTube's DOM is not an API. This client therefore verifies playlist/video identity from
    exact URL parameters before and after writes, and raises when the current UI cannot be
    identified safely instead of guessing.
    """

    def __init__(
        self,
        *,
        user_data_dir: str | Path = DEFAULT_BROWSER_PROFILE,
        headless: bool = False,
        channel: str | None = None,
        save_label: str = "Save",
        create_playlist_label: str = "New playlist",
        create_label: str = "Create",
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
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "browser execution support is not installed; run `pip install -e '.[browser]'` "
                "and `playwright install chromium`"
            ) from exc

        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = {
            "user_data_dir": str(Path(user_data_dir)),
            "headless": headless,
        }
        if channel:
            kwargs["channel"] = channel
        self._context = self._pw.chromium.launch_persistent_context(**kwargs)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.save_label = save_label
        self.create_playlist_label = create_playlist_label
        self.create_label = create_label
        self.max_scrolls = max_scrolls
        self.scroll_pause = scroll_pause
        self.stable_rounds = stable_rounds
        self._known: dict[str, BrowserPlaylist] = {}

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._pw.stop()

    def _scroll_until_stable(self, selector: str) -> None:
        stable = 0
        last_height = -1
        last_count = -1
        for _ in range(self.max_scrolls):
            count = self._page.locator(selector).count()
            height = int(
                self._page.evaluate(
                    "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                )
            )
            if count == last_count and height == last_height:
                stable += 1
            else:
                stable = 0
            if stable >= self.stable_rounds:
                return
            last_count, last_height = count, height
            self._page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            if self.scroll_pause:
                self._page.wait_for_timeout(int(self.scroll_pause * 1000))

    def list_playlists(self) -> list[BrowserPlaylist]:
        self._page.goto(PLAYLISTS_URL, wait_until="domcontentloaded")
        self._page.wait_for_timeout(700)
        selector = 'a[href*="/playlist?list="]'
        self._scroll_until_stable(selector)

        found: dict[str, BrowserPlaylist] = {}
        links = self._page.locator(selector)
        for index in range(links.count()):
            link = links.nth(index)
            playlist_id = _playlist_id_from_href(link.get_attribute("href"))
            if not playlist_id or playlist_id in {"WL", "LL"}:
                continue
            title = (link.get_attribute("title") or link.inner_text() or "").strip()
            if not title:
                continue
            row = BrowserPlaylist(playlist_id=playlist_id, title=title, privacy_status="unknown")
            existing = found.get(playlist_id)
            if existing is not None and existing.title != title:
                raise RuntimeError(
                    f"playlist id {playlist_id!r} appeared with conflicting titles "
                    f"{existing.title!r} and {title!r}"
                )
            found[playlist_id] = row

        self._known = found
        return sorted(found.values(), key=lambda row: (row.title.casefold(), row.playlist_id))

    def _playlist_title(self, playlist_id: str) -> str:
        row = self._known.get(playlist_id)
        if row is None:
            self.list_playlists()
            row = self._known.get(playlist_id)
        if row is None:
            raise RuntimeError(f"playlist id {playlist_id!r} is not present in the live browser account")
        return row.title

    def find_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
            raise ValueError(f"invalid YouTube video id {video_id!r}")
        self._playlist_title(playlist_id)
        self._page.goto(PLAYLIST_URL.format(playlist_id=playlist_id), wait_until="domcontentloaded")
        self._page.wait_for_timeout(500)
        selector = 'ytd-playlist-video-renderer a[href*="watch"]'
        self._scroll_until_stable(selector)
        anchors = self._page.locator(selector)
        for index in range(anchors.count()):
            if _video_id_from_href(anchors.nth(index).get_attribute("href")) == video_id:
                return BrowserPlaylistItem()
        return None

    def create_playlist(self, title: str, privacy_status: str) -> BrowserPlaylist:
        if not title.strip():
            raise ValueError("playlist title must not be empty")
        if privacy_status not in {"private", "unlisted", "public"}:
            raise ValueError("privacy_status must be private, unlisted or public")

        before = self.list_playlists()
        matches = [row for row in before if row.title.casefold() == title.casefold()]
        if matches:
            if len(matches) > 1:
                raise RuntimeError(f"playlist title {title!r} is ambiguous before creation")
            return matches[0]

        button = self._page.get_by_text(self.create_playlist_label, exact=True)
        if button.count() == 0:
            button = self._page.get_by_role("button", name=self.create_playlist_label, exact=True)
        if button.count() == 0:
            raise RuntimeError(
                f"could not find playlist creation control {self.create_playlist_label!r}; "
                "YouTube UI or localization may have changed"
            )
        button.first.click()

        dialog = self._page.locator("ytd-dialog-renderer, tp-yt-paper-dialog").last
        textbox = dialog.get_by_role("textbox")
        if textbox.count() == 0:
            textbox = dialog.locator("input, textarea")
        if textbox.count() == 0:
            raise RuntimeError("could not find playlist title field in creation dialog")
        textbox.first.fill(title)

        # Privacy controls have changed several times. Prefer role-based names, but fail
        # rather than assuming a default when a non-private setting was requested.
        if privacy_status != "private":
            desired = privacy_status.capitalize()
            combo = dialog.get_by_role("combobox")
            if combo.count() == 0:
                raise RuntimeError(
                    f"could not safely select playlist privacy {privacy_status!r}; "
                    "create it manually or use private"
                )
            combo.first.click()
            option = self._page.get_by_text(desired, exact=True)
            if option.count() == 0:
                raise RuntimeError(f"could not find privacy option {desired!r}")
            option.first.click()

        create = dialog.get_by_role("button", name=self.create_label, exact=True)
        if create.count() == 0:
            create = dialog.get_by_text(self.create_label, exact=True)
        if create.count() == 0:
            raise RuntimeError(f"could not find playlist create control {self.create_label!r}")
        create.first.click()
        self._page.wait_for_timeout(800)

        after = self.list_playlists()
        matches = [row for row in after if row.title.casefold() == title.casefold()]
        if len(matches) != 1:
            raise RuntimeError(
                f"playlist creation click did not yield exactly one live playlist titled {title!r}"
            )
        return BrowserPlaylist(
            playlist_id=matches[0].playlist_id,
            title=matches[0].title,
            privacy_status=privacy_status,
        )

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem:
        title = self._playlist_title(playlist_id)
        if self.find_playlist_item(playlist_id, video_id) is not None:
            return BrowserPlaylistItem()

        self._page.goto(WATCH_URL.format(video_id=video_id), wait_until="domcontentloaded")
        self._page.wait_for_timeout(600)
        current = _video_id_from_href(self._page.url)
        if current != video_id:
            raise RuntimeError(
                f"watch page identity mismatch before save: expected {video_id!r}, found {current!r}"
            )

        save = self._page.get_by_role("button", name=self.save_label, exact=True)
        if save.count() == 0:
            save = self._page.get_by_text(self.save_label, exact=True)
        if save.count() == 0:
            raise RuntimeError(
                f"could not find save-to-playlist control {self.save_label!r}; "
                "YouTube UI or localization may have changed"
            )
        save.first.click()

        dialog = self._page.locator("ytd-add-to-playlist-renderer, ytd-dialog-renderer").last
        rows = dialog.locator("ytd-playlist-add-to-option-renderer")
        matching = []
        for index in range(rows.count()):
            row = rows.nth(index)
            text = (row.inner_text() or "").strip()
            first_line = text.splitlines()[0].strip() if text else ""
            if first_line.casefold() == title.casefold():
                matching.append(row)
        if len(matching) != 1:
            self._page.keyboard.press("Escape")
            raise RuntimeError(
                f"save dialog did not contain exactly one destination titled {title!r}"
            )

        current = _video_id_from_href(self._page.url)
        if current != video_id:
            self._page.keyboard.press("Escape")
            raise RuntimeError(
                f"watch page identity changed before playlist insertion: expected {video_id!r}, found {current!r}"
            )

        row = matching[0]
        checkbox = row.locator("tp-yt-paper-checkbox, #checkbox")
        checked = False
        if checkbox.count():
            checked = (checkbox.first.get_attribute("aria-checked") or "").lower() == "true"
        if not checked:
            row.click()
            self._page.wait_for_timeout(500)
        self._page.keyboard.press("Escape")

        # The dialog click is not considered success until the exact video is observed in
        # the exact destination playlist.
        if self.find_playlist_item(playlist_id, video_id) is None:
            raise RuntimeError(
                f"save action for {video_id!r} did not produce confirmed membership in playlist {playlist_id!r}"
            )
        return BrowserPlaylistItem()
