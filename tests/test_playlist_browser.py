from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.playlist_browser import (
    BrowserPlaylist,
    BrowserPlaylistItem,
    execute_browser_plan,
)
from youtube_watchlater_tidy.playlist_sync import INVENTORY_FORMAT, create_plan, import_inventory, plan_payload
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class FakePlaylistBrowser:
    def __init__(self) -> None:
        self.playlists = {
            "PL_EXIST": BrowserPlaylist("PL_EXIST", "Queue - Existing", "private"),
        }
        self.items: dict[str, set[str]] = {"PL_EXIST": {"video00000A"}}
        self.create_calls: list[tuple[str, str]] = []
        self.insert_calls: list[tuple[str, str]] = []
        self.fail_once_insert: set[str] = set()
        self.failed_once: set[str] = set()

    def list_playlists(self) -> list[BrowserPlaylist]:
        return list(self.playlists.values())

    def find_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem | None:
        if video_id in self.items.get(playlist_id, set()):
            return BrowserPlaylistItem(f"BPI-{playlist_id}-{video_id}")
        return None

    def create_playlist(self, title: str, privacy_status: str) -> BrowserPlaylist:
        self.create_calls.append((title, privacy_status))
        playlist_id = f"PL_NEW_{len(self.create_calls)}"
        row = BrowserPlaylist(playlist_id, title, privacy_status)
        self.playlists[playlist_id] = row
        self.items[playlist_id] = set()
        return row

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem:
        if video_id in self.fail_once_insert and video_id not in self.failed_once:
            self.failed_once.add(video_id)
            raise RuntimeError("simulated transient browser failure")
        self.insert_calls.append((playlist_id, video_id))
        self.items.setdefault(playlist_id, set()).add(video_id)
        return BrowserPlaylistItem(f"BPI-{playlist_id}-{video_id}")


class BrowserPlaylistExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "catalogue.sqlite3"
        source = root / "watch-later.json"
        source.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "entries": [
                        {"id": "video00000A", "title": "Already there"},
                        {"id": "video00000B", "title": "Needs existing"},
                        {"id": "video00000C", "title": "Needs new"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            for title, destination in (
                ("Already there", "Queue - Existing"),
                ("Needs existing", "Queue - Existing"),
                ("Needs new", "Queue - New"),
            ):
                selection = select_title(conn, contains=title, snapshot_id=self.snapshot)
                apply_selection_action(
                    conn,
                    "move",
                    destination_playlist=destination,
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
                            "playlist_id": "PL_EXIST",
                            "title": "Queue - Existing",
                            "privacy_status": "private",
                            "items": [
                                {"video_id": "video00000A", "playlist_item_id": "OLD-A"}
                            ],
                        }
                    ],
                },
            )
            self.plan = create_plan(conn, self.snapshot, backend="browser")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_does_not_use_browser_or_mutate(self) -> None:
        with open_catalogue(self.db_path) as conn:
            before = plan_payload(conn, self.plan.run_id)
            result = execute_browser_plan(conn, self.plan.run_id)
            after = plan_payload(conn, self.plan.run_id)
        self.assertFalse(result.applied)
        self.assertEqual(before, after)
        self.assertEqual(result.writes, 0)

    def test_apply_rechecks_membership_creates_and_inserts(self) -> None:
        client = FakePlaylistBrowser()
        with open_catalogue(self.db_path) as conn:
            result = execute_browser_plan(
                conn,
                self.plan.run_id,
                client=client,
                apply=True,
                interval=0,
                backoff=0,
            )
            payload = plan_payload(conn, self.plan.run_id)
        self.assertEqual(result.already_present, 1)
        self.assertEqual(result.created_playlists, 1)
        self.assertEqual(result.inserted, 2)
        self.assertEqual(result.writes, 3)
        self.assertEqual(result.run_status, "complete")
        self.assertEqual(client.create_calls, [("Queue - New", "private")])
        items = {row["video_id"]: row for row in payload["items"]}
        self.assertEqual(items["video00000A"]["status"], "already_present")
        self.assertIsNotNone(items["video00000A"]["attempted_at"])
        self.assertEqual(items["video00000B"]["status"], "inserted")
        self.assertEqual(items["video00000C"]["status"], "inserted")

    def test_write_cap_resumes_without_recreating_destination(self) -> None:
        client = FakePlaylistBrowser()
        with open_catalogue(self.db_path) as conn:
            first = execute_browser_plan(
                conn,
                self.plan.run_id,
                client=client,
                apply=True,
                max_writes=1,
                interval=0,
                backoff=0,
            )
            second = execute_browser_plan(
                conn,
                self.plan.run_id,
                client=client,
                apply=True,
                interval=0,
                backoff=0,
            )
        self.assertEqual(first.writes, 1)
        self.assertEqual(first.run_status, "partial")
        self.assertEqual(second.run_status, "complete")
        self.assertEqual(len(client.create_calls), 1)

    def test_transient_insert_is_retried(self) -> None:
        client = FakePlaylistBrowser()
        client.fail_once_insert.add("video00000B")
        with open_catalogue(self.db_path) as conn:
            result = execute_browser_plan(
                conn,
                self.plan.run_id,
                client=client,
                apply=True,
                retries=1,
                interval=0,
                backoff=0,
            )
        self.assertEqual(result.failed, 0)
        self.assertIn(("PL_EXIST", "video00000B"), client.insert_calls)

    def test_stale_plan_refuses_before_browser_access(self) -> None:
        client = FakePlaylistBrowser()
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Needs existing", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            with self.assertRaisesRegex(ValueError, "stale"):
                execute_browser_plan(conn, self.plan.run_id, client=client, apply=True)
        self.assertEqual(client.create_calls, [])
        self.assertEqual(client.insert_calls, [])

    def test_known_destination_disappearing_is_failure_not_recreation(self) -> None:
        client = FakePlaylistBrowser()
        del client.playlists["PL_EXIST"]
        client.items.pop("PL_EXIST", None)
        with open_catalogue(self.db_path) as conn:
            result = execute_browser_plan(
                conn,
                self.plan.run_id,
                client=client,
                apply=True,
                interval=0,
                backoff=0,
            )
            payload = plan_payload(conn, self.plan.run_id)
        self.assertGreaterEqual(result.failed, 1)
        existing = next(row for row in payload["items"] if row["video_id"] == "video00000A")
        self.assertEqual(existing["status"], "failed")
        self.assertIn("not present", existing["error"])


if __name__ == "__main__":
    unittest.main()
