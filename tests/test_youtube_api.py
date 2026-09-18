from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.playlist_sync import (
    INVENTORY_FORMAT,
    create_plan,
    import_inventory,
    plan_payload,
)
from youtube_watchlater_tidy.triage import apply_selection_action, select_title
from youtube_watchlater_tidy.youtube_api import (
    execute_api_plan,
    fetch_inventory_from_api,
    refresh_inventory,
)


class FakeYouTubeClient:
    def __init__(self) -> None:
        self.playlists = {
            "PL_EXIST": self._playlist("PL_EXIST", "Queue - Existing", "private"),
            "PL_OTHER": self._playlist("PL_OTHER", "Other", "unlisted"),
        }
        self.items: dict[str, dict[str, dict]] = {
            "PL_EXIST": {
                # Live account has A even though the imported planning inventory below does not.
                "video00000A": self._item("PLI_A", "PL_EXIST", "video00000A", 0),
            },
            "PL_OTHER": {},
        }
        self.create_calls: list[tuple[str, str]] = []
        self.insert_calls: list[tuple[str, str]] = []

    @staticmethod
    def _playlist(playlist_id: str, title: str, privacy: str) -> dict:
        return {
            "id": playlist_id,
            "snippet": {"title": title},
            "status": {"privacyStatus": privacy},
        }

    @staticmethod
    def _item(item_id: str, playlist_id: str, video_id: str, position: int) -> dict:
        return {
            "id": item_id,
            "snippet": {
                "playlistId": playlist_id,
                "position": position,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            },
            "status": {"privacyStatus": "private"},
        }

    def list_playlists(self) -> list[dict]:
        return list(self.playlists.values())

    def list_playlist_items(self, playlist_id: str) -> list[dict]:
        return list(self.items.get(playlist_id, {}).values())

    def find_playlist_item(self, playlist_id: str, video_id: str) -> dict | None:
        return self.items.get(playlist_id, {}).get(video_id)

    def create_playlist(self, title: str, privacy_status: str) -> dict:
        self.create_calls.append((title, privacy_status))
        playlist_id = f"PL_NEW_{len(self.create_calls)}"
        row = self._playlist(playlist_id, title, privacy_status)
        self.playlists[playlist_id] = row
        self.items[playlist_id] = {}
        return row

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> dict:
        self.insert_calls.append((playlist_id, video_id))
        item_id = f"PLI_NEW_{len(self.insert_calls)}"
        row = self._item(item_id, playlist_id, video_id, len(self.items.setdefault(playlist_id, {})))
        self.items[playlist_id][video_id] = row
        return row


class YouTubeApiExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalogue.sqlite3"
        source = self.root / "watch-later.json"
        source.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "entries": [
                        {"id": "video00000A", "title": "Existing destination"},
                        {"id": "video00000C", "title": "Needs new destination"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            existing = select_title(conn, contains="Existing destination", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Existing",
                selection_id=existing.selection_id,
            )
            new = select_title(conn, contains="Needs new destination", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - New",
                selection_id=new.selection_id,
            )
            # Planning inventory intentionally misses video A to verify the executor's live duplicate check.
            import_inventory(
                conn,
                {
                    "format": INVENTORY_FORMAT,
                    "source": "test-stale-inventory",
                    "fetched_at": "2026-09-15T12:00:00+00:00",
                    "playlists": [
                        {
                            "playlist_id": "PL_EXIST",
                            "title": "Queue - Existing",
                            "privacy_status": "private",
                            "items": [],
                        }
                    ],
                },
            )
            self.plan = create_plan(conn, self.snapshot, backend="api")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_api_inventory_fetch_and_refresh(self) -> None:
        client = FakeYouTubeClient()
        payload = fetch_inventory_from_api(client, show_progress=False)
        self.assertEqual(payload["format"], INVENTORY_FORMAT)
        self.assertEqual(payload["source"], "youtube-data-api")
        playlists = {row["playlist_id"]: row for row in payload["playlists"]}
        self.assertEqual(playlists["PL_EXIST"]["title"], "Queue - Existing")
        self.assertEqual(playlists["PL_EXIST"]["items"][0]["video_id"], "video00000A")

        with open_catalogue(self.db_path) as conn:
            result = refresh_inventory(conn, client, show_progress=False)
            self.assertEqual((result.playlists, result.items), (2, 1))

    def test_execute_is_dry_run_without_apply_or_api_client(self) -> None:
        with open_catalogue(self.db_path) as conn:
            before = plan_payload(conn, self.plan.run_id)
            result = execute_api_plan(conn, self.plan.run_id)
            after = plan_payload(conn, self.plan.run_id)

        self.assertFalse(result.applied)
        self.assertEqual(result.writes, 0)
        self.assertEqual(before, after)

    def test_apply_live_checks_duplicates_creates_destination_and_checkpoints(self) -> None:
        client = FakeYouTubeClient()
        events = []
        with open_catalogue(self.db_path) as conn:
            result = execute_api_plan(
                conn,
                self.plan.run_id,
                client=client,
                apply=True,
                progress=events.append,
            )
            payload = plan_payload(conn, self.plan.run_id)

        self.assertTrue(result.applied)
        self.assertEqual(result.created_playlists, 1)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.already_present, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.writes, 2)  # one playlist create + one item insert
        self.assertEqual(result.run_status, "complete")
        self.assertEqual(client.create_calls, [("Queue - New", "private")])
        self.assertEqual(len(client.insert_calls), 1)
        items = {row["video_id"]: row for row in payload["items"]}
        self.assertEqual(items["video00000A"]["status"], "already_present")
        self.assertEqual(items["video00000A"]["playlist_item_id"], "PLI_A")
        self.assertEqual(items["video00000C"]["status"], "inserted")
        destination = next(
            row for row in payload["destinations"] if row["destination_name"] == "Queue - New"
        )
        self.assertEqual(destination["status"], "created")
        self.assertTrue(destination["destination_playlist_id"].startswith("PL_NEW_"))
        self.assertEqual(events[0].kind, "status")
        self.assertEqual(next(event for event in events if event.kind == "start").total, 2)
        self.assertEqual(events[-1].kind, "finish")
        self.assertEqual(events[-1].completed, 2)

    def test_planner_already_present_is_rechecked_live_before_success(self) -> None:
        # Re-plan from an inventory which says A is present, then simulate it disappearing
        # before execution. Execution must insert it rather than trust old inventory state.
        with open_catalogue(self.db_path) as conn:
            import_inventory(
                conn,
                {
                    "format": INVENTORY_FORMAT,
                    "source": "test-present-at-plan-time",
                    "fetched_at": "2026-09-15T12:30:00+00:00",
                    "playlists": [
                        {
                            "playlist_id": "PL_EXIST",
                            "title": "Queue - Existing",
                            "privacy_status": "private",
                            "items": [
                                {
                                    "video_id": "video00000A",
                                    "playlist_item_id": "PLI_OLD",
                                    "position": 0,
                                }
                            ],
                        }
                    ],
                },
            )
            plan = create_plan(conn, self.snapshot, backend="api")

            client = FakeYouTubeClient()
            client.items["PL_EXIST"].clear()
            result = execute_api_plan(conn, plan.run_id, client=client, apply=True)
            payload = plan_payload(conn, plan.run_id)

        self.assertEqual(result.run_status, "complete")
        self.assertIn(("PL_EXIST", "video00000A"), client.insert_calls)
        item = next(row for row in payload["items"] if row["video_id"] == "video00000A")
        self.assertEqual(item["status"], "inserted")
        self.assertIsNotNone(item["attempted_at"])

    def test_resume_after_write_cap_does_not_repeat_successful_creation(self) -> None:
        client = FakeYouTubeClient()
        with open_catalogue(self.db_path) as conn:
            first = execute_api_plan(
                conn,
                self.plan.run_id,
                client=client,
                apply=True,
                max_writes=1,
            )
            self.assertEqual(first.writes, 1)
            self.assertEqual(first.created_playlists, 1)
            self.assertEqual(first.inserted, 0)
            self.assertEqual(first.run_status, "partial")

            second = execute_api_plan(conn, self.plan.run_id, client=client, apply=True)
            payload = plan_payload(conn, self.plan.run_id)

        self.assertEqual(len(client.create_calls), 1)
        self.assertEqual(second.created_playlists, 0)
        self.assertEqual(second.inserted, 1)
        self.assertEqual(second.run_status, "complete")
        self.assertEqual(payload["status"], "complete")

    def test_apply_refuses_stale_decision_before_network(self) -> None:
        client = FakeYouTubeClient()
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Existing destination", snapshot_id=self.snapshot)
            apply_selection_action(conn, "archive", selection_id=selection.selection_id)
            with self.assertRaisesRegex(ValueError, "stale"):
                execute_api_plan(conn, self.plan.run_id, client=client, apply=True)

        self.assertEqual(client.create_calls, [])
        self.assertEqual(client.insert_calls, [])

    def test_apply_refuses_over_quota_plan_unless_overridden(self) -> None:
        client = FakeYouTubeClient()
        with open_catalogue(self.db_path) as conn:
            plan = create_plan(conn, self.snapshot, backend="api", quota_limit=1)
            with self.assertRaisesRegex(ValueError, "quota"):
                execute_api_plan(conn, plan.run_id, client=client, apply=True)
            # Explicit override is allowed; live checks may reduce actual writes below the estimate.
            result = execute_api_plan(
                conn,
                plan.run_id,
                client=client,
                apply=True,
                allow_over_quota=True,
            )
        self.assertTrue(result.applied)

    def test_known_destination_disappearing_is_failure_not_silent_recreation(self) -> None:
        client = FakeYouTubeClient()
        del client.playlists["PL_EXIST"]
        client.items.pop("PL_EXIST", None)
        with open_catalogue(self.db_path) as conn:
            result = execute_api_plan(conn, self.plan.run_id, client=client, apply=True)
            payload = plan_payload(conn, self.plan.run_id)

        self.assertGreaterEqual(result.failed, 1)
        self.assertEqual(client.create_calls[0][0], "Queue - New")
        existing = next(row for row in payload["items"] if row["video_id"] == "video00000A")
        self.assertEqual(existing["status"], "failed")
        self.assertIn("no longer present", existing["error"])


if __name__ == "__main__":
    unittest.main()
