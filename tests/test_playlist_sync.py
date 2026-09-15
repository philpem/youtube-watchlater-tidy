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
    inventory_payload,
    plan_payload,
)
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class PlaylistSyncPlannerTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "Existing A"},
                        {"id": "video00000B", "title": "Existing B"},
                        {"id": "video00000C", "title": "New C"},
                        {"id": "video00000D", "title": "No move D"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            existing = select_title(conn, contains="Existing", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Existing",
                selection_id=existing.selection_id,
            )
            new = select_title(conn, contains="New C", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - New",
                selection_id=new.selection_id,
            )
            import_inventory(conn, self._inventory())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _inventory() -> dict:
        return {
            "format": INVENTORY_FORMAT,
            "source": "api-test",
            "fetched_at": "2026-09-15T15:00:00+00:00",
            "playlists": [
                {
                    "playlist_id": "PL_EXIST",
                    "title": "Queue - Existing",
                    "privacy_status": "private",
                    "items": [
                        {
                            "video_id": "video00000B",
                            "playlist_item_id": "PLI_B",
                            "position": 4,
                        }
                    ],
                },
                {
                    "playlist_id": "PL_OTHER",
                    "title": "Unrelated",
                    "privacy_status": "unlisted",
                    "items": [],
                },
            ],
        }

    def test_api_plan_resolves_existing_creates_missing_and_estimates_quota(self) -> None:
        with open_catalogue(self.db_path) as conn:
            plan = create_plan(conn, self.snapshot, backend="api")
            payload = plan_payload(conn, plan.run_id)

        self.assertEqual(plan.destination_count, 2)
        self.assertEqual(plan.create_count, 1)
        self.assertEqual(plan.insert_count, 2)
        self.assertEqual(plan.already_present_count, 1)
        self.assertEqual(plan.estimated_quota, 150)
        self.assertFalse(plan.exceeds_quota)

        destinations = {row["destination_name"]: row for row in payload["destinations"]}
        self.assertEqual(destinations["Queue - Existing"]["status"], "existing")
        self.assertEqual(destinations["Queue - Existing"]["destination_playlist_id"], "PL_EXIST")
        self.assertEqual(destinations["Queue - New"]["status"], "create_planned")
        self.assertIsNone(destinations["Queue - New"]["destination_playlist_id"])
        self.assertEqual(destinations["Queue - New"]["privacy_status"], "private")

        items = {row["video_id"]: row for row in payload["items"]}
        self.assertEqual(items["video00000A"]["status"], "planned")
        self.assertEqual(items["video00000A"]["destination_playlist_id"], "PL_EXIST")
        self.assertEqual(items["video00000B"]["status"], "already_present")
        self.assertEqual(items["video00000B"]["playlist_item_id"], "PLI_B")
        self.assertEqual(items["video00000C"]["status"], "planned")
        self.assertNotIn("video00000D", items)
        self.assertEqual(payload["stale_item_count"], 0)

    def test_browser_plan_has_same_operations_but_no_api_quota(self) -> None:
        with open_catalogue(self.db_path) as conn:
            plan = create_plan(conn, self.snapshot, backend="browser", quota_limit=1)
        self.assertEqual(plan.insert_count, 2)
        self.assertEqual(plan.create_count, 1)
        self.assertEqual(plan.estimated_quota, 0)
        self.assertFalse(plan.exceeds_quota)

    def test_api_quota_exceeded_is_persisted_in_plan(self) -> None:
        with open_catalogue(self.db_path) as conn:
            plan = create_plan(conn, self.snapshot, backend="api", quota_limit=100)
            payload = plan_payload(conn, plan.run_id)
        self.assertTrue(plan.exceeds_quota)
        self.assertEqual(payload["quota"]["estimated"], 150)
        self.assertEqual(payload["quota"]["limit"], 100)
        self.assertTrue(payload["quota"]["exceeds"])

    def test_plan_becomes_stale_when_underlying_human_decision_changes(self) -> None:
        with open_catalogue(self.db_path) as conn:
            plan = create_plan(conn, self.snapshot)
            selection = select_title(conn, contains="Existing A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "archive", selection_id=selection.selection_id)
            payload = plan_payload(conn, plan.run_id)

        stale = {row["video_id"]: row["stale"] for row in payload["items"]}
        self.assertTrue(stale["video00000A"])
        self.assertFalse(stale["video00000B"])
        self.assertFalse(stale["video00000C"])
        self.assertEqual(payload["stale_item_count"], 1)

    def test_ambiguous_destination_title_is_rejected(self) -> None:
        inventory = self._inventory()
        inventory["playlists"].append(
            {
                "playlist_id": "PL_DUP",
                "title": "Queue - Existing",
                "privacy_status": "private",
                "items": [],
            }
        )
        with open_catalogue(self.db_path) as conn:
            import_inventory(conn, inventory)
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                create_plan(conn, self.snapshot)

    def test_inventory_is_required_before_planning(self) -> None:
        other_db = self.root / "no-inventory.sqlite3"
        other_source = self.root / "other.json"
        other_source.write_text(
            json.dumps({"id": "WL", "entries": [{"id": "x", "title": "X"}]}),
            encoding="utf-8",
        )
        with open_catalogue(other_db) as conn:
            snapshot = import_watchlater_json(conn, other_source).snapshot_id
            selection = select_title(conn, contains="X", snapshot_id=snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - X",
                selection_id=selection.selection_id,
            )
            with self.assertRaisesRegex(ValueError, "no playlist inventory"):
                create_plan(conn, snapshot)

    def test_inventory_round_trip_preserves_contents(self) -> None:
        with open_catalogue(self.db_path) as conn:
            payload = inventory_payload(conn)
        self.assertEqual(payload["format"], INVENTORY_FORMAT)
        self.assertEqual(payload["source"], "api-test")
        playlists = {row["playlist_id"]: row for row in payload["playlists"]}
        self.assertEqual(playlists["PL_EXIST"]["title"], "Queue - Existing")
        self.assertEqual(playlists["PL_EXIST"]["items"][0]["video_id"], "video00000B")


if __name__ == "__main__":
    unittest.main()
