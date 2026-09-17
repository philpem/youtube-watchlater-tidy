from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.multi_destination import (
    destinations_for_decision,
    ensure_decision_destinations_schema,
    record_selection_move,
)
from youtube_watchlater_tidy.multi_destination import create_plan
from youtube_watchlater_tidy.multi_destination_support import (
    create_removal_plan,
    plan_payload,
)
from youtube_watchlater_tidy.playlist_browser import (
    BrowserPlaylist,
    BrowserPlaylistItem,
    execute_browser_plan,
)
from youtube_watchlater_tidy.playlist_sync import INVENTORY_FORMAT, import_inventory
from youtube_watchlater_tidy.triage import apply_selection_action, select_title
from youtube_watchlater_tidy.watchlater_removal import removal_plan_payload
from youtube_watchlater_tidy.youtube_api import execute_api_plan


class FakeApi:
    def __init__(self) -> None:
        self.playlists = {
            "PL_A": self._playlist("PL_A", "Queue A"),
            "PL_B": self._playlist("PL_B", "Queue B"),
        }
        self.items = {"PL_A": {}, "PL_B": {}}
        self.insert_calls: list[tuple[str, str]] = []

    @staticmethod
    def _playlist(playlist_id: str, title: str) -> dict:
        return {
            "id": playlist_id,
            "snippet": {"title": title},
            "status": {"privacyStatus": "private"},
        }

    @staticmethod
    def _item(item_id: str, playlist_id: str, video_id: str) -> dict:
        return {
            "id": item_id,
            "snippet": {
                "playlistId": playlist_id,
                "position": 0,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            },
            "status": {"privacyStatus": "private"},
        }

    def list_playlists(self) -> list[dict]:
        return list(self.playlists.values())

    def list_playlist_items(self, playlist_id: str) -> list[dict]:
        return list(self.items[playlist_id].values())

    def find_playlist_item(self, playlist_id: str, video_id: str) -> dict | None:
        return self.items[playlist_id].get(video_id)

    def create_playlist(self, title: str, privacy_status: str) -> dict:
        raise AssertionError("test inventory already contains both destinations")

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> dict:
        self.insert_calls.append((playlist_id, video_id))
        item = self._item(f"PLI_{len(self.insert_calls)}", playlist_id, video_id)
        self.items[playlist_id][video_id] = item
        return item


class FakeBrowser:
    def __init__(self) -> None:
        self.playlists = [
            BrowserPlaylist("PL_A", "Queue A", "private"),
            BrowserPlaylist("PL_B", "Queue B", "private"),
        ]
        self.items: set[tuple[str, str]] = set()
        self.insert_calls: list[tuple[str, str]] = []

    def list_playlists(self) -> list[BrowserPlaylist]:
        return list(self.playlists)

    def find_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem | None:
        if (playlist_id, video_id) in self.items:
            return BrowserPlaylistItem(f"browser:{playlist_id}:{video_id}")
        return None

    def create_playlist(self, title: str, privacy_status: str) -> BrowserPlaylist:
        raise AssertionError("test inventory already contains both destinations")

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> BrowserPlaylistItem:
        self.insert_calls.append((playlist_id, video_id))
        self.items.add((playlist_id, video_id))
        return BrowserPlaylistItem(f"browser:{playlist_id}:{video_id}")


class MultiDestinationTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "Target A"},
                        {"id": "video00000B", "title": "Legacy B"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            self.selection = select_title(
                conn, contains="Target A", snapshot_id=self.snapshot
            ).selection_id
            legacy = select_title(
                conn, contains="Legacy B", snapshot_id=self.snapshot
            ).selection_id
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue A",
                selection_id=legacy,
            )
            import_inventory(
                conn,
                {
                    "format": INVENTORY_FORMAT,
                    "source": "multi-test",
                    "fetched_at": "2026-09-17T12:00:00+00:00",
                    "playlists": [
                        {
                            "playlist_id": "PL_A",
                            "title": "Queue A",
                            "privacy_status": "private",
                            "items": [],
                        },
                        {
                            "playlist_id": "PL_B",
                            "title": "Queue B",
                            "privacy_status": "private",
                            "items": [],
                        },
                    ],
                },
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record_multi(self) -> int:
        with open_catalogue(self.db_path) as conn:
            count = record_selection_move(
                conn,
                destinations=["Queue A", "Queue B", "queue a"],
                selection_id=self.selection,
            )
            self.assertEqual(count, 1)
            row = conn.execute(
                """
                SELECT id FROM current_decisions
                WHERE snapshot_id=? AND video_id='video00000A'
                """,
                (self.snapshot,),
            ).fetchone()
            assert row is not None
            return int(row["id"])

    def test_existing_single_destination_decision_is_backfilled(self) -> None:
        with open_catalogue(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id FROM current_decisions
                WHERE snapshot_id=? AND video_id='video00000B'
                """,
                (self.snapshot,),
            ).fetchone()
            assert row is not None
            ensure_decision_destinations_schema(conn)
            self.assertEqual(destinations_for_decision(conn, int(row["id"])), ("Queue A",))

    def test_assignment_preserves_primary_and_deduplicates_destinations(self) -> None:
        event_id = self._record_multi()
        with open_catalogue(self.db_path) as conn:
            row = conn.execute(
                "SELECT destination_playlist FROM decision_events WHERE id=?",
                (event_id,),
            ).fetchone()
            assert row is not None
            self.assertEqual(row["destination_playlist"], "Queue A")
            self.assertEqual(
                destinations_for_decision(conn, event_id),
                ("Queue A", "Queue B"),
            )

    def test_planner_expands_one_video_into_two_destination_items(self) -> None:
        self._record_multi()
        with open_catalogue(self.db_path) as conn:
            plan = create_plan(conn, self.snapshot, backend="api")
            payload = plan_payload(conn, plan.run_id)

        rows = [row for row in payload["items"] if row["video_id"] == "video00000A"]
        self.assertEqual([row["destination_name"] for row in rows], ["Queue A", "Queue B"])
        self.assertEqual(payload["stale_item_count"], 0)
        # Target A contributes two inserts; legacy B contributes one Queue A insert.
        self.assertEqual(plan.insert_count, 3)
        self.assertEqual(plan.estimated_quota, 150)

    def test_api_executor_authorizes_secondary_destination(self) -> None:
        self._record_multi()
        client = FakeApi()
        with open_catalogue(self.db_path) as conn:
            # Archive the legacy video so this plan only exercises the multi move.
            legacy = select_title(conn, contains="Legacy B", snapshot_id=self.snapshot)
            apply_selection_action(conn, "archive", selection_id=legacy.selection_id)
            plan = create_plan(conn, self.snapshot, backend="api")
            result = execute_api_plan(conn, plan.run_id, client=client, apply=True)
            payload = plan_payload(conn, plan.run_id)

        self.assertEqual(result.inserted, 2)
        self.assertEqual(result.stale, 0)
        self.assertEqual(result.run_status, "complete")
        self.assertCountEqual(
            client.insert_calls,
            [("PL_A", "video00000A"), ("PL_B", "video00000A")],
        )
        self.assertEqual(payload["stale_item_count"], 0)

    def test_browser_executor_authorizes_secondary_destination(self) -> None:
        self._record_multi()
        client = FakeBrowser()
        with open_catalogue(self.db_path) as conn:
            legacy = select_title(conn, contains="Legacy B", snapshot_id=self.snapshot)
            apply_selection_action(conn, "archive", selection_id=legacy.selection_id)
            plan = create_plan(conn, self.snapshot, backend="browser")
            result = execute_browser_plan(
                conn,
                plan.run_id,
                client=client,
                apply=True,
                interval=0,
                retries=0,
                backoff=0,
            )

        self.assertEqual(result.inserted, 2)
        self.assertEqual(result.stale, 0)
        self.assertEqual(result.run_status, "complete")
        self.assertCountEqual(
            client.insert_calls,
            [("PL_A", "video00000A"), ("PL_B", "video00000A")],
        )

    def test_watchlater_removal_waits_for_every_destination(self) -> None:
        event_id = self._record_multi()
        with open_catalogue(self.db_path) as conn:
            legacy = select_title(conn, contains="Legacy B", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=legacy.selection_id)
            sync = create_plan(conn, self.snapshot, backend="api")
            rows = conn.execute(
                """
                SELECT ordinal, destination_name
                FROM playlist_sync_items
                WHERE run_id=? AND video_id='video00000A'
                ORDER BY ordinal
                """,
                (sync.run_id,),
            ).fetchall()
            self.assertEqual(len(rows), 2)

            first = rows[0]
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_sync_items
                    SET status='inserted', attempted_at='2026-09-17T12:01:00+00:00',
                        completed_at='2026-09-17T12:01:00+00:00'
                    WHERE run_id=? AND ordinal=?
                    """,
                    (sync.run_id, first["ordinal"]),
                )
            blocked = create_removal_plan(conn, self.snapshot)
            blocked_payload = removal_plan_payload(conn, blocked.run_id)
            self.assertEqual(blocked.eligible_count, 0)
            self.assertEqual(blocked.blocked_move_count, 1)
            self.assertEqual(
                blocked_payload["blocked_moves"][0]["missing_destinations"],
                ["Queue B"],
            )

            second = rows[1]
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_sync_items
                    SET status='already_present', attempted_at='2026-09-17T12:02:00+00:00',
                        completed_at='2026-09-17T12:02:00+00:00'
                    WHERE run_id=? AND ordinal=?
                    """,
                    (sync.run_id, second["ordinal"]),
                )
            allowed = create_removal_plan(conn, self.snapshot)

        self.assertEqual(allowed.eligible_count, 1)
        self.assertEqual(allowed.move_count, 1)
        self.assertEqual(allowed.blocked_move_count, 0)
        self.assertGreater(event_id, 0)


if __name__ == "__main__":
    unittest.main()
