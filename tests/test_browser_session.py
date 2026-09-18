from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.browser_session import PlaywrightBrowserSession


class FakePage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, *, pages=None) -> None:
        self.pages = list(pages or [])
        self.closed = False
        self.new_pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        page = FakePage()
        self.new_pages.append(page)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, contexts) -> None:
        self.contexts = contexts
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self) -> None:
        self.connected_endpoint = None
        self.browser = FakeBrowser([FakeContext()])
        self.launch_kwargs = None
        self.launched_context = FakeContext(pages=[FakePage()])

    def connect_over_cdp(self, endpoint):
        self.connected_endpoint = endpoint
        return self.browser

    def launch_persistent_context(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.launched_context


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def start(self) -> FakePlaywright:
        return self.playwright


class BrowserSessionTests(unittest.TestCase):
    def _module(self, playwright: FakePlaywright):
        return types.SimpleNamespace(sync_playwright=lambda: FakeStarter(playwright))

    def test_cdp_mode_attaches_to_existing_context_and_owns_only_new_page(self) -> None:
        fake = FakePlaywright()
        with patch.dict(sys.modules, {"playwright.sync_api": self._module(fake)}):
            session = PlaywrightBrowserSession(
                user_data_dir=Path("ignored"),
                cdp_endpoint="http://127.0.0.1:9222",
            )
            page = session.page
            self.assertEqual(fake.chromium.connected_endpoint, "http://127.0.0.1:9222")
            self.assertEqual(len(fake.chromium.browser.contexts[0].new_pages), 1)
            session.close()

        self.assertTrue(page.closed)
        self.assertTrue(fake.chromium.browser.closed)
        self.assertFalse(fake.chromium.browser.contexts[0].closed)
        self.assertTrue(fake.stopped)

    def test_launch_mode_preserves_existing_persistent_profile_behavior(self) -> None:
        fake = FakePlaywright()
        with patch.dict(sys.modules, {"playwright.sync_api": self._module(fake)}):
            session = PlaywrightBrowserSession(
                user_data_dir=Path("/tmp/watchlater-profile"),
                headless=True,
                channel="chrome",
            )
            self.assertEqual(
                fake.chromium.launch_kwargs,
                {
                    "user_data_dir": "/tmp/watchlater-profile",
                    "headless": True,
                    "channel": "chrome",
                },
            )
            self.assertIs(session.page, fake.chromium.launched_context.pages[0])
            session.close()

        self.assertTrue(fake.chromium.launched_context.closed)
        self.assertTrue(fake.stopped)

    def test_headless_is_rejected_for_attached_browser(self) -> None:
        with self.assertRaisesRegex(ValueError, "headless"):
            PlaywrightBrowserSession(
                user_data_dir=Path("ignored"),
                headless=True,
                cdp_endpoint="http://127.0.0.1:9222",
            )


if __name__ == "__main__":
    unittest.main()
