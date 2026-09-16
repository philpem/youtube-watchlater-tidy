from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.triage import apply_selection_action, select_title
from youtube_watchlater_tidy.watchlater_browser import BrowserRemovalAttempt, execute_removal_plan
from youtube_watchlater_tidy.watchlater_removal import create_removal_plan, removal_plan_payload


class FakeBrowser:
    def __init__(self, outcomes: dict[str, list[str] | str]) -> None:
        self.outcomes = {
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


class WatchLaterBrowserExecutorTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "Delete A"},
                        {"id": "video00000B", "title": "Archive B"},
                        {"id": "video00000C", "title": "Delete C"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            delete_a = select_title(conn, contains="Delete A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "delete", selection_id=delete_a.selection_id)
            archive_b = select_title(conn, contains="Archive B", snapshot_id=self.snapshot)
            apply_selection_action(conn, "archive", selection_id=archive_b.selection_id)
            delete_c = select_title(conn, contains="Delete C", snapshot_id=self.snapshot)
            apply_selection_action(conn, "delete", selection_id=delete_c.selection_id)
            self.plan = create_removal_plan(conn, self.snapshot)

    def tearDown(self) -> None:
        self.tmp.cleanup()

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

    def test_outcomes_checkpoint_and_not_found_is_retriable(self) -> None:
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
            self.assertEqual(first.run_status, "partial")
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
            payload = removal_plan_payload(conn, self.plan.run_id)
        self.assertEqual((first.removed, first.already_absent, first.not_found), (1, 1, 1))
        self.assertEqual(retry.calls, ["video00000C"])
        self.assertEqual(second.run_status, "complete")
        self.assertEqual(payload["status"], "complete")

    def test_max_deletes_caps_clicks_and_resume_skips_completed(self) -> None:
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

    def test_stale_plan_refuses_before_browser_call(self) -> None:
        fake = FakeBrowser({"video00000A": "removed"})
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Delete A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            with self.assertRaisesRegex(ValueError, "stale"):
                execute_removal_plan(
                    conn,
                    self.plan.run_id,
                    client=fake,
                    apply=True,
                    confirmed=True,
                )
        self.assertEqual(fake.calls, [])

    def test_exception_is_checkpointed_failed_after_retries(self) -> None:
        fake = FakeBrowser({"video00000A": "raise", "video00000B": "already_absent", "video00000C": "already_absent"})
        with open_catalogue(self.db_path) as conn:
            result = execute_removal_plan(
                conn,
                self.plan.run_id,
                client=fake,
                apply=True,
                confirmed=True,
                retries=0,
                interval=0,
                backoff=0,
            )
            payload = removal_plan_payload(conn, self.plan.run_id)
        self.assertEqual(result.failed, 1)
        item = next(row for row in payload["items"] if row["video_id"] == "video00000A")
        self.assertEqual(item["status"], "failed")
        self.assertIn("simulated browser failure", item["error"])


if __name__ == "__main__":
    unittest.main()
