from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .playlist_browser import BrowserPlaylist, BrowserPlaylistItem
from .watchlater_browser import DEFAULT_BROWSER_PROFILE

PLAYLISTS_URL = "https://www.youtube.com/feed/playlists"
STUDIO_URL = "https://studio.youtube.com/"
PLAYLIST_URL = "https://www.youtube.com/playlist?list={playlist_id}"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


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
    """Authenticated YouTube playlist UI adapter using a persistent profile.

    YouTube's DOM is not an API. Every write is anchored to an exact playlist/video ID and
    the adapter raises when it cannot prove the intended identity rather than guessing.
    Playwright is imported lazily so core installs do not need the browser extra.
    """

    def __init__(
        self,
        *,
        user_data_dir: str | Path = DEFAULT_BROWSER_PROFILE,
        headless: bool = False,
        channel: str | None = None,
        max_scrolls: int = 250,
        scroll_pause: float = 0.7,
        stable_rounds: int = 4,
        create_label: str = "Create",
        new_playlist_label: str = "New playlist",
        add_label: str = "Add",
        save_to_playlist_label: str = "Save to playlist",
        studio_save_label: str = "Save",
        visibility_label: str = "Visibility",
        search_label: str = "Search",
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
        launch: dict[str, Any] = {
            "user_data_dir": str(Path(user_data_dir)),
            "headless": headless,
        }
        if channel:
            launch["channel"] = channel
        self._context = self._pw.chromium.launch_persistent_context(**launch)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.max_scrolls = max_scrolls
        self.scroll_pause = scroll_pause
        self.stable_rounds = stable_rounds
        self.create_label = create_label
        self.new_playlist_label = new_playlist_label
        self.add_label = add_label
        self.save_to_playlist_label = save_to_playlist_label
        self.studio_save_label = studio_save_label
        self.visibility_label = visibility_label
        self.search_label = search_label

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._pw.stop()

    def _goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")
        self._page.wait_for_timeout(700)

    def _scroll_to_stable(self, locator) -> bool:
        stable = 0
        last_height = -1
        last_count = -1
        for _ in range(self.max_scrolls):
            count = locator.count()
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
                return True
            last_count, last_height = count, height
            self._page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            if self.scroll_pause:
                self._page.wait_for_timeout(int(self.scroll_pause * 1000))
        return False

    @staticmethod
    def _anchor_title(anchor) -> str | None:
        try:
            card = anchor.locator(
                "xpath=ancestor::*[self::yt-lockup-view-model or self::ytd-grid-playlist-renderer][1]"
            )
            if card.count():
                for selector in ("h3", "#video-title", "yt-formatted-string"):
                    candidate = card.locator(selector).first
                    if candidate.count():
                        text = candidate.inner_text().strip()
                        if text:
                            return text.splitlines()[0].strip()
        except Exception:
            pass

        value = anchor.get_attribute("title")
        if value and value.strip():
            return value.strip()
        try:
            text = anchor.inner_text().strip()
        except Exception:
            text = ""
        if text:
            return text.splitlines()[0].strip()
        value = anchor.get_attribute("aria-label")
        if value and value.strip() and value.strip().casefold() not in {
            "play all",
            "view full playlist",
        }:
            return value.strip()
        return None

    def list_playlists(self) -> list[BrowserPlaylist]:
        self._goto(PLAYLISTS_URL)
        anchors = self._page.locator('a[href*="playlist?list="]')
        complete = self._scroll_to_stable(anchors)
        if not complete:
            raise RuntimeError(
                "playlist listing did not reach a stable end before --max-scrolls; refusing an incomplete live inventory"
            )

        found: dict[str, BrowserPlaylist] = {}
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            playlist_id = _playlist_id_from_href(anchor.get_attribute("href"))
            if not playlist_id or playlist_id in {"WL", "LL"} or playlist_id.startswith("RD"):
                continue
            title = self._anchor_title(anchor)
            if not title:
                continue
            previous = found.get(playlist_id)
            if previous is not None and previous.title != title:
                raise RuntimeError(
                    f"playlist {playlist_id!r} appeared with conflicting live titles "
                    f"{previous.title!r} and {title!r}; refusing to guess"
                )
            found[playlist_id] = BrowserPlaylist(playlist_id=playlist_id, title=title)
        return sorted(found.values(), key=lambda row: (row.title.casefold(), row.playlist_id))

    def _matching_video_row(self, video_id: str):
        rows = self._page.locator("ytd-playlist-video-renderer, yt-lockup-view-model")
        for index in range(rows.count()):
            row = rows.nth(index)
            anchors = row.locator('a[href*="watch"]')
            for anchor_index in range(anchors.count()):
                href = anchors.nth(anchor_index).get_attribute("href")
                if _video_id_from_href(href) == video_id:
                    return row
        return None

    def _find_video_in_playlist(self, playlist_id: str, video_id: str):
        self._goto(PLAYLIST_URL.format(playlist_id=playlist_id))
        if _playlist_id_from_href(self._page.url) != playlist_id:
            raise RuntimeError(
                f"playlist navigation identity mismatch: expected {playlist_id!r}, got {self._page.url!r}"
            )
        rows = self._page.locator("ytd-playlist-video-renderer, yt-lockup-view-model")
        stable = 0
        last_height = -1
        last_count = -1
        for _ in range(self.max_scrolls):
            row = self._matching_video_row(video_id)
            if row is not None:
                return row, True
            count = rows.count()
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
                return None, True
            last_count, last_height = count, height
            self._page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            if self.scroll_pause:
                self._page.wait_for_timeout(int(self.scroll_pause * 1000))
        return None, False

    def find_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem | None:
        row, complete = self._find_video_in_playlist(playlist_id, video_id)
        if row is not None:
            return BrowserPlaylistItem()
        if not complete:
            raise RuntimeError(
                f"could not establish whether {video_id!r} is in playlist {playlist_id!r} before --max-scrolls"
            )
        return None

    def _click_text_or_role(self, scope, label: str) -> None:
        button = scope.get_by_role("button", name=label, exact=True)
        if button.count():
            button.first.click()
            return
        text = scope.get_by_text(label, exact=True)
        if text.count():
            text.first.click()
            return
        raise RuntimeError(f"could not find YouTube UI control {label!r}")

    def create_playlist(self, title: str, privacy_status: str) -> BrowserPlaylist:
        if not title.strip():
            raise ValueError("playlist title cannot be empty")
        if privacy_status not in {"private", "unlisted", "public"}:
            raise ValueError("playlist privacy must be private, unlisted or public")

        # YouTube documents playlist creation in Studio as Create -> New playlist.
        self._goto(STUDIO_URL)
        self._click_text_or_role(self._page, self.create_label)
        self._click_text_or_role(self._page, self.new_playlist_label)
        dialog = self._page.get_by_role("dialog")
        scope = dialog.last if dialog.count() else self._page

        textbox = scope.get_by_role("textbox")
        if textbox.count() == 0:
            raise RuntimeError("could not find playlist title field in YouTube Studio")
        textbox.first.fill(title)

        combo = scope.get_by_role("combobox")
        if combo.count():
            combo.first.click()
        else:
            visibility = scope.get_by_text(self.visibility_label, exact=False)
            if visibility.count() == 0:
                raise RuntimeError("could not find playlist visibility control in YouTube Studio")
            visibility.first.click()
        privacy_label = privacy_status.capitalize()
        option = self._page.get_by_text(privacy_label, exact=True)
        if option.count() == 0:
            raise RuntimeError(
                f"could not find visibility option {privacy_label!r}; YouTube locale/UI may have changed"
            )
        option.last.click()
        self._click_text_or_role(scope, self.studio_save_label)
        self._page.wait_for_timeout(1000)

        matches: list[BrowserPlaylist] = []
        for _ in range(3):
            matches = [row for row in self.list_playlists() if row.title == title]
            if matches:
                break
            self._page.wait_for_timeout(1000)
        if len(matches) != 1:
            raise RuntimeError(
                f"created playlist {title!r} but could not uniquely rediscover it in the live account "
                f"({len(matches)} matching playlists)"
            )
        row = matches[0]
        return BrowserPlaylist(row.playlist_id, row.title, privacy_status)

    def _result_row_for_video(self, scope, video_id: str):
        anchors = scope.locator('a[href*="watch"]')
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            if _video_id_from_href(anchor.get_attribute("href")) != video_id:
                continue
            row = anchor.locator(
                "xpath=ancestor::*[self::ytd-video-renderer or self::yt-lockup-view-model or @role='option'][1]"
            )
            return row if row.count() else anchor
        return None

    def _add_search_box(self, scope):
        search = scope.get_by_placeholder(self.search_label, exact=False)
        if search.count():
            return search.first
        search_tab = scope.get_by_text(self.search_label, exact=True)
        if search_tab.count():
            search_tab.first.click()
            self._page.wait_for_timeout(250)
            search = scope.get_by_placeholder(self.search_label, exact=False)
            if search.count():
                return search.first
        textbox = scope.get_by_role("textbox")
        if textbox.count() == 1:
            return textbox.first
        raise RuntimeError(
            "could not establish the Add-videos Search field; refusing to type into an ambiguous textbox"
        )

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
            raise ValueError(f"invalid YouTube video id {video_id!r}")
        # Re-check exact membership immediately before the destination write.
        existing = self.find_playlist_item(playlist_id, video_id)
        if existing is not None:
            return existing

        if _playlist_id_from_href(self._page.url) != playlist_id:
            raise RuntimeError("playlist identity changed immediately before Add")
        self._click_text_or_role(self._page, self.add_label)
        dialog = self._page.get_by_role("dialog")
        scope = dialog.last if dialog.count() else self._page

        search = self._add_search_box(scope)
        search.fill(WATCH_URL.format(video_id=video_id))
        search.press("Enter")
        self._page.wait_for_timeout(800)

        result = self._result_row_for_video(scope, video_id)
        if result is None:
            raise RuntimeError(
                f"Add-videos search did not return the exact requested video id {video_id!r}"
            )
        checkbox = result.get_by_role("checkbox")
        if checkbox.count():
            checkbox.first.click()
        else:
            result.click()

        # Verify the destination page identity again immediately before the final write.
        if _playlist_id_from_href(self._page.url) != playlist_id:
            raise RuntimeError(
                f"playlist identity changed before Save to playlist: expected {playlist_id!r}, got {self._page.url!r}"
            )
        self._click_text_or_role(scope, self.save_to_playlist_label)
        self._page.wait_for_timeout(1000)

        confirmed = self.find_playlist_item(playlist_id, video_id)
        if confirmed is None:
            raise RuntimeError(
                f"YouTube accepted the add flow for {video_id!r}, but exact destination membership was not confirmed"
            )
        return confirmed
