from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.playlist_sync import INVENTORY_FORMAT, create_plan, import_inventory
from youtube_watchlater_tidy.triage import apply_selection_action, select_title
from youtube_watchlater_tidy.watchlater_removal import (
    checkpoint_removal,
    create_removal_plan,
    finish_removal_run,
    pending_removal_items,
    removal_item_is_authorized,
    removal_plan_payload,
)


class WatchLaterRemovalPlanTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "Delete A"},
                        {"id": "video00000B", "title": "Archive B"},
                        {"id": "video00000C", "title": "Move confirmed C"},
                        {"id": "video00000D", "title": "Move blocked D"},
                        {"id": "video00000E", "title": "Keep E"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            for title, action, destination in (
                ("Delete A", "delete", None),
                ("Archive B", "archive", None),
                ("Move confirmed C", "move", "Queue - Confirmed"),
                ("Move blocked D", "move", "Queue - Blocked"),
                ("Keep E", "keep", None),
            ):
                selection = select_title(conn, contains=title, snapshot_id=self.snapshot)
                apply_selection_action(
                    conn,
                    action,
                    destination_playlist=destination,
                    selection_id=selection.selection_id,
                )

            import_inventory(
                conn,
                {
                    "format": INVENTORY_FORMAT,
                    "source": "test",
                    "fetched_at": "2026-09-16T18:00:00+00:00",
                    "playlists": [
                        {
                            "playlist_id": "PL_C",
                            "title": "Queue - Confirmed",
                            "privacy_status": "private",
                            "items": [],
                        },
                        {
                            "playlist_id": "PL_D",
                            "title": "Queue - Blocked",
                            "privacy_status": "private",
                            "items": [],
                        },
                    ],
                },
            )
            sync = create_plan(conn, self.snapshot, backend="api")
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_sync_items
                    SET status='inserted', attempted_at='2026-09-16T18:05:00+00:00',
                        completed_at='2026-09-16T18:05:00+00:00', playlist_item_id='PLI_C'
                    WHERE run_id=? AND video_id='video00000C'
                    """,
                    (sync.run_id,),
                )
            self.plan = create_removal_plan(conn, self.snapshot)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_plan_includes_delete_archive_and_only_confirmed_move(self) -> None:
        with open_catalogue(self.db_path) as conn:
            payload = removal_plan_payload(conn, self.plan.run_id)

        self.assertEqual(self.plan.eligible_count, 3)
        self.assertEqual(self.plan.delete_count, 1)
        self.assertEqual(self.plan.archive_count, 1)
        self.assertEqual(self.plan.move_count, 1)
        self.assertEqual(self.plan.blocked_move_count, 1)

        items = {row["video_id"]: row for row in payload["items"]}
        self.assertEqual(set(items), {"video00000A", "video00000B", "video00000C"})
        self.assertIsNotNone(items["video00000C"]["destination_sync_run_id"])
        self.assertEqual(payload["blocked_moves"][0]["video_id"], "video00000D")
        self.assertNotIn("video00000E", items)

    def test_inventory_only_already_present_is_not_enough_for_move_removal(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Move blocked D", snapshot_id=self.snapshot)
            current = conn.execute(
                "SELECT id FROM current_decisions WHERE snapshot_id=? AND video_id='video00000D'",
                (self.snapshot,),
            ).fetchone()
            sync = conn.execute(
                """
                SELECT run_id, ordinal FROM playlist_sync_items
                WHERE video_id='video00000D' ORDER BY run_id DESC LIMIT 1
                """
            ).fetchone()
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_sync_items
                    SET status='already_present', attempted_at=NULL, completed_at=NULL
                    WHERE run_id=? AND ordinal=? AND decision_event_id=?
                    """,
                    (sync["run_id"], sync["ordinal"], current["id"]),
                )
            plan = create_removal_plan(conn, self.snapshot)

        self.assertEqual(plan.blocked_move_count, 1)
        self.assertEqual(plan.move_count, 1)
        self.assertEqual(selection.entry_count, 1)

    def test_live_verified_already_present_allows_move_removal(self) -> None:
        with open_catalogue(self.db_path) as conn:
            current = conn.execute(
                "SELECT id FROM current_decisions WHERE snapshot_id=? AND video_id='video00000D'",
                (self.snapshot,),
            ).fetchone()
            sync = conn.execute(
                """
                SELECT run_id, ordinal FROM playlist_sync_items
                WHERE video_id='video00000D' ORDER BY run_id DESC LIMIT 1
                """
            ).fetchone()
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_sync_items
                    SET status='already_present', attempted_at='2026-09-16T18:06:00+00:00',
                        completed_at='2026-09-16T18:06:00+00:00'
                    WHERE run_id=? AND ordinal=? AND decision_event_id=?
                    """,
                    (sync["run_id"], sync["ordinal"], current["id"]),
                )
            plan = create_removal_plan(conn, self.snapshot)

        self.assertEqual(plan.blocked_move_count, 0)
        self.assertEqual(plan.move_count, 2)

    def test_plan_becomes_stale_when_current_decision_changes(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Delete A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            payload = removal_plan_payload(conn, self.plan.run_id)

        stale = {row["video_id"]: row["stale"] for row in payload["items"]}
        self.assertTrue(stale["video00000A"])
        self.assertFalse(stale["video00000B"])

    def test_move_plan_becomes_stale_if_destination_decision_changes(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Move confirmed C", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Different",
                selection_id=selection.selection_id,
            )
            payload = removal_plan_payload(conn, self.plan.run_id)

        move = next(row for row in payload["items"] if row["video_id"] == "video00000C")
        self.assertTrue(move["stale"])

    def test_checkpoint_states_support_resume_without_touching_snapshot(self) -> None:
        with open_catalogue(self.db_path) as conn:
            run, pending = pending_removal_items(conn, self.plan.run_id)
            self.assertEqual(len(pending), 3)
            self.assertTrue(removal_item_is_authorized(conn, run, pending[0]))

            checkpoint_removal(
                conn,
                run_id=self.plan.run_id,
                ordinal=1,
                status="removed",
            )
            checkpoint_removal(
                conn,
                run_id=self.plan.run_id,
                ordinal=2,
                status="not_found",
                error="not visible in current page scan",
            )
            status, remaining = finish_removal_run(conn, self.plan.run_id)
            _, retry = pending_removal_items(conn, self.plan.run_id)
            source_title = conn.execute(
                "SELECT title FROM snapshot_entries WHERE snapshot_id=? AND video_id='video00000A'",
                (self.snapshot,),
            ).fetchone()["title"]

        self.assertEqual(status, "partial")
        self.assertEqual(remaining, 2)
        self.assertEqual([row["video_id"] for row in retry], ["video00000B", "video00000C"])
        self.assertEqual(source_title, "Delete A")


if __name__ == "__main__":
    unittest.main()
