from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.playlist_sync import INVENTORY_FORMAT, create_plan, import_inventory
from youtube_watchlater_tidy.triage import apply_selection_action, select_title
from youtube_watchlater_tidy.watchlater_browser import (
    BrowserRemovalAttempt,
    execute_removal_plan,
)
from youtube_watchlater_tidy.watchlater_removal import (
    create_removal_plan,
    removal_plan_payload,
)


class FakeBrowser:
    def __init__(self, outcomes: dict[str, list[str] | str]) -> None:
        self.outcomes: dict[str, list[str]] = {
            video_id: value if isinstance(value, list) else [value]
            for video_id, value in outcomes.items()
        }
        self.calls: list[str] = []

    def remove_video(self, video_id: str) -> BrowserRemovalAttempt:
        self.calls.append(video_id)
        values = self.outcomes.setdefault(video_id, ["already_absent"])
        status = values.pop(0) if len(values) > 1 else values[0]
        if status == "raise":
            raise RuntimeError("simulated browser failure")
        return BrowserRemovalAttempt(status)

    def close(self) -> None:
        pass


class WatchLaterRemovalTests(unittest.TestCase):
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
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            delete = select_title(conn, contains="Delete A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "delete", selection_id=delete.selection_id)
            archive = select_title(conn, contains="Archive B", snapshot_id=self.snapshot)
            apply_selection_action(conn, "archive", selection_id=archive.selection_id)
            move_c = select_title(conn, contains="Move confirmed C", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Confirmed",
                selection_id=move_c.selection_id,
            )
            move_d = select_title(conn, contains="Move blocked D", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Blocked",
                selection_id=move_d.selection_id,
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
            # Confirm only C. D deliberately remains planned.
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

    def test_plan_becomes_stale_when_decision_changes(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Delete A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            payload = removal_plan_payload(conn, self.plan.run_id)
        stale = {row["video_id"]: row["stale"] for row in payload["items"]}
        self.assertTrue(stale["video00000A"])
        self.assertFalse(stale["video00000B"])

    def test_dry_run_makes_no_browser_calls_or_checkpoint_changes(self) -> None:
        fake = FakeBrowser({"video00000A": "removed"})
        with open_catalogue(self.db_path) as conn:
            before = removal_plan_payload(conn, self.plan.run_id)
            result = execute_removal_plan(conn, self.plan.run_id, client=fake)
            after = removal_plan_payload(conn, self.plan.run_id)
        self.assertFalse(result.applied)
        self.assertEqual(fake.calls, [])
        self.assertEqual(before, after)

    def test_apply_requires_second_destructive_confirmation(self) -> None:
        fake = FakeBrowser({"video00000A": "removed"})
        with open_catalogue(self.db_path) as conn:
            with self.assertRaisesRegex(ValueError, "confirm-remove"):
                execute_removal_plan(
                    conn,
                    self.plan.run_id,
                    client=fake,
                    apply=True,
                    confirmed=False,
                )
        self.assertEqual(fake.calls, [])

    def test_outcomes_are_checkpointed_and_not_found_is_retriable(self) -> None:
        fake = FakeBrowser(
            {
                "video00000A": "removed",
                "video00000B": "already_absent",
                "video00000C": "not_found",
            }
        )
        with open_catalogue(self.db_path) as conn:
            first = execute_removal_plan(
                conn,
                self.plan.run_id,
                client=fake,
                apply=True,
                confirmed=True,
                retries=0,
                interval=0,
                backoff=0,
            )
            first_payload = removal_plan_payload(conn, self.plan.run_id)

            retry = FakeBrowser({"video00000C": "removed"})
            second = execute_removal_plan(
                conn,
                self.plan.run_id,
                client=retry,
                apply=True,
                confirmed=True,
                retries=0,
                interval=0,
                backoff=0,
            )
            final_payload = removal_plan_payload(conn, self.plan.run_id)

        self.assertEqual((first.removed, first.already_absent, first.not_found), (1, 1, 1))
        self.assertEqual(first.run_status, "partial")
        first_status = {row["video_id"]: row["status"] for row in first_payload["items"]}
        self.assertEqual(first_status["video00000C"], "not_found")
        self.assertEqual(retry.calls, ["video00000C"])
        self.assertEqual(second.removed, 1)
        self.assertEqual(second.run_status, "complete")
        self.assertEqual(final_payload["status"], "complete")

    def test_max_deletes_caps_destructive_clicks_and_resume_skips_completed(self) -> None:
        fake = FakeBrowser(
            {
                "video00000A": "removed",
                "video00000B": "removed",
                "video00000C": "removed",
            }
        )
        with open_catalogue(self.db_path) as conn:
            first = execute_removal_plan(
                conn,
                self.plan.run_id,
                client=fake,
                apply=True,
                confirmed=True,
                max_deletes=1,
                retries=0,
                interval=0,
                backoff=0,
            )
            self.assertEqual(first.destructive_actions, 1)
            self.assertEqual(fake.calls, ["video00000A"])
            second = execute_removal_plan(
                conn,
                self.plan.run_id,
                client=fake,
                apply=True,
                confirmed=True,
                max_deletes=10,
                retries=0,
                interval=0,
                backoff=0,
            )
        self.assertEqual(second.removed, 2)
        self.assertEqual(fake.calls, ["video00000A", "video00000B", "video00000C"])
        self.assertEqual(second.run_status, "complete")

    def test_move_becomes_stale_if_confirmation_no_longer_matches_current_decision(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
