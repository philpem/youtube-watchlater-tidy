from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"


class PlaywrightBrowserSession:
    """Own one Playwright-controlled page in either a launched or attached Chromium session."""

    def __init__(
        self,
        *,
        user_data_dir: str | Path,
        headless: bool = False,
        channel: str | None = None,
        cdp_endpoint: str | None = None,
    ) -> None:
        if cdp_endpoint and headless:
            raise ValueError("--headless cannot be used with --cdp-endpoint; launch the browser headless yourself")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "browser execution support is not installed; run `pip install -e '.[browser]'` "
                "and `playwright install chromium`"
            ) from exc

        self._pw = sync_playwright().start()
        self._browser = None
        self._context = None
        self._page = None
        self._attached = bool(cdp_endpoint)

        try:
            if cdp_endpoint:
                self._browser = self._pw.chromium.connect_over_cdp(cdp_endpoint)
                contexts = self._browser.contexts
                if not contexts:
                    raise RuntimeError(
                        f"browser at {cdp_endpoint!r} exposed no Chromium browser context"
                    )
                self._context = contexts[0]
                # Use a fresh tab so the executor does not take over one the user is browsing in.
                self._page = self._context.new_page()
            else:
                kwargs: dict[str, Any] = {
                    "user_data_dir": str(Path(user_data_dir)),
                    "headless": headless,
                }
                if channel:
                    kwargs["channel"] = channel
                self._context = self._pw.chromium.launch_persistent_context(**kwargs)
                self._page = (
                    self._context.pages[0]
                    if self._context.pages
                    else self._context.new_page()
                )
        except Exception:
            self._pw.stop()
            raise

    @property
    def context(self):
        return self._context

    @property
    def page(self):
        return self._page

    def close(self) -> None:
        try:
            if self._attached:
                # The page belongs to us; the surrounding browser belongs to the user.
                if self._page is not None:
                    try:
                        self._page.close()
                    except Exception:
                        pass
                if self._browser is not None:
                    self._browser.close()
            elif self._context is not None:
                self._context.close()
        finally:
            self._pw.stop()
