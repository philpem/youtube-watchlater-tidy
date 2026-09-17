from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.playlist_playwright import _playlist_id_from_href, _video_id_from_href
from youtube_watchlater_tidy.playlist_sync import INVENTORY_FORMAT, create_plan, import_inventory
from youtube_watchlater_tidy.playlist_sync_cli import main
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class FakePlaywrightClient:
    instances: list["FakePlaywrightClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    def list_playlists(self):
        from youtube_watchlater_tidy.playlist_browser import BrowserPlaylist

        return [BrowserPlaylist("PL_EXISTING", "Queue - Existing")]

    def find_playlist_item(self, playlist_id, video_id):
        return None

    def create_playlist(self, title, privacy_status):
        raise AssertionError("not expected in this test")

    def insert_playlist_item(self, playlist_id, video_id):
        from youtube_watchlater_tidy.playlist_browser import BrowserPlaylistItem

        return BrowserPlaylistItem()

    def close(self):
        self.closed = True


class PlaylistPlaywrightTests(unittest.TestCase):
    def setUp(self) -> None:
        FakePlaywrightClient.instances.clear()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "catalogue.sqlite3"
        source = root / "watch-later.json"
        source.write_text(
            json.dumps({"id": "WL", "entries": [{"id": "video00000A", "title": "Move A"}]}),
            encoding="utf-8",
        )
        with open_catalogue(self.db) as conn:
            snapshot = import_watchlater_json(conn, source).snapshot_id
            selection = select_title(conn, contains="Move A", snapshot_id=snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Existing",
                selection_id=selection.selection_id,
            )
            import_inventory(
                conn,
                {
                    "format": INVENTORY_FORMAT,
                    "source": "test",
                    "fetched_at": "2026-09-17T12:00:00+00:00",
                    "playlists": [
                        {
                            "playlist_id": "PL_EXISTING",
                            "title": "Queue - Existing",
                            "privacy_status": "private",
                            "items": [],
                        }
                    ],
                },
            )
            self.plan = create_plan(conn, snapshot, backend="browser")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_url_identity_parsers_require_exact_query_parameters(self) -> None:
        self.assertEqual(_playlist_id_from_href("/playlist?list=PL123&si=x"), "PL123")
        self.assertEqual(_video_id_from_href("/watch?v=abcDEF_1234&list=PL123"), "abcDEF_1234")
        self.assertIsNone(_playlist_id_from_href("/watch?v=PL123"))
        self.assertIsNone(_video_id_from_href("/playlist?list=abcDEF_1234"))
        self.assertIsNone(_video_id_from_href(None))

    def test_browser_dry_run_does_not_construct_playwright_client(self) -> None:
        with patch(
            "youtube_watchlater_tidy.playlist_sync_cli.PlaywrightPlaylistClient",
            FakePlaywrightClient,
        ):
            rc = main(["--db", str(self.db), "execute", "--run-id", str(self.plan.run_id)])
        self.assertEqual(rc, 0)
        self.assertEqual(FakePlaywrightClient.instances, [])

    def test_browser_apply_constructs_and_closes_client(self) -> None:
        with patch(
            "youtube_watchlater_tidy.playlist_sync_cli.PlaywrightPlaylistClient",
            FakePlaywrightClient,
        ):
            rc = main(
                [
                    "--db",
                    str(self.db),
                    "execute",
                    "--run-id",
                    str(self.plan.run_id),
                    "--apply",
                    "--interval",
                    "0",
                    "--retries",
                    "0",
                    "--user-data-dir",
                    str(Path(self.tmp.name) / "profile"),
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(FakePlaywrightClient.instances), 1)
        self.assertTrue(FakePlaywrightClient.instances[0].closed)

    def test_stale_browser_plan_fails_before_client_construction(self) -> None:
        with open_catalogue(self.db) as conn:
            selection = select_title(conn, contains="Move A")
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
        with patch(
            "youtube_watchlater_tidy.playlist_sync_cli.PlaywrightPlaylistClient",
            FakePlaywrightClient,
        ):
            rc = main(
                [
                    "--db",
                    str(self.db),
                    "execute",
                    "--run-id",
                    str(self.plan.run_id),
                    "--apply",
                ]
            )
        self.assertEqual(rc, 2)
        self.assertEqual(FakePlaywrightClient.instances, [])


if __name__ == "__main__":
    unittest.main()
