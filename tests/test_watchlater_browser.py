from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.triage import apply_selection_action, select_title
from youtube_watchlater_tidy.watchlater_remove_cli import main as removal_cli_main
from youtube_watchlater_tidy.watchlater_browser import (
    BrowserRemovalAttempt,
    BrowserScanEvent,
    PlaywrightWatchLaterClient,
    _ScrollMetrics,
    execute_removal_plan,
)
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


class FakeBatchBrowser:
    def __init__(self, loaded_ids: list[str], *, complete: bool = True) -> None:
        self.loaded_ids = loaded_ids
        self.complete = complete
        self.scan_calls: list[set[str]] = []
        self.remove_calls: list[str] = []

    def scan_matching_videos(self, video_ids: set[str]):
        self.scan_calls.append(set(video_ids))
        matched = []
        for video_id in self.loaded_ids:
            if video_id in video_ids:
                matched.append(video_id)
                yield BrowserScanEvent("candidate", video_id=video_id)
        remaining = tuple(sorted(video_ids - set(matched)))
        yield BrowserScanEvent("finished", remaining_video_ids=remaining, complete=self.complete)

    def remove_loaded_video(self, video_id: str) -> BrowserRemovalAttempt:
        self.remove_calls.append(video_id)
        return BrowserRemovalAttempt("removed")

    def remove_video(self, video_id: str) -> BrowserRemovalAttempt:
        raise AssertionError("single-video scan must not be used by a batch client")

    def close(self) -> None:
        pass


class ScriptedScanClient(PlaywrightWatchLaterClient):
    def __init__(
        self,
        loaded_rounds: list[list[str]],
        metrics: list[_ScrollMetrics],
        *,
        stable_rounds: int = 2,
    ) -> None:
        self.loaded_rounds = list(loaded_rounds)
        self.metrics = list(metrics)
        self.max_scrolls = 20
        self.scroll_pause = 0
        self.stable_rounds = stable_rounds
        self.progress = None
        self.scroll_calls = 0

    def _navigate_watch_later_start(self) -> None:
        pass

    def _loaded_video_ids(self) -> list[str]:
        if len(self.loaded_rounds) > 1:
            return self.loaded_rounds.pop(0)
        return self.loaded_rounds[0]

    def _scroll_metrics(self) -> _ScrollMetrics:
        if len(self.metrics) > 1:
            return self.metrics.pop(0)
        return self.metrics[0]

    def _scroll_to_bottom(self) -> None:
        self.scroll_calls += 1


class BulkRowsLocator:
    def __init__(self, hrefs: list[str | None]) -> None:
        self.hrefs = hrefs
        self.evaluate_all_calls = 0

    def evaluate_all(self, script: str):
        self.evaluate_all_calls += 1
        self.script = script
        return list(self.hrefs)


class BulkRowsPage:
    def __init__(self, locator: BulkRowsLocator) -> None:
        self.rows = locator
        self.selectors: list[str] = []

    def locator(self, selector: str) -> BulkRowsLocator:
        self.selectors.append(selector)
        return self.rows


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

    def test_cli_execute_prints_concise_summary_by_default(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            rc = removal_cli_main(
                ["--db", str(self.db_path), "execute", "--run-id", str(self.plan.run_id)]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            output.getvalue(),
            f"Removal run {self.plan.run_id}: applied=no, status=planned, removed=0, "
            "already_absent=0, not_found=0, failed=0, stale=0, remaining=3\n",
        )

    def test_cli_execute_json_preserves_full_machine_readable_output(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            rc = removal_cli_main(
                [
                    "--db",
                    str(self.db_path),
                    "execute",
                    "--run-id",
                    str(self.plan.run_id),
                    "--json",
                ]
            )

        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["execution"]["run_id"], self.plan.run_id)
        self.assertFalse(payload["execution"]["applied"])
        self.assertEqual(len(payload["plan"]["items"]), 3)

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

    def test_batch_client_removes_loaded_matches_in_one_scan(self) -> None:
        fake = FakeBatchBrowser(["video00000C", "unplanned001", "video00000A"])
        progress: list[str] = []
        events = []
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
                progress=progress.append,
                progress_events=events.append,
            )
            payload = removal_plan_payload(conn, self.plan.run_id)

        self.assertEqual(len(fake.scan_calls), 1)
        self.assertEqual(fake.remove_calls, ["video00000C", "video00000A"])
        self.assertEqual(result.removed, 2)
        self.assertEqual(result.already_absent, 1)
        self.assertEqual(result.run_status, "complete")
        statuses = {row["video_id"]: row["status"] for row in payload["items"]}
        self.assertEqual(statuses["video00000B"], "already_absent")
        self.assertTrue(any("checkpoint saved" in line for line in progress))
        self.assertEqual(events[0].kind, "start")
        self.assertEqual(events[0].total, 3)
        self.assertEqual(events[-1].kind, "finish")
        self.assertEqual(events[-1].completed, 3)

    def test_incomplete_batch_scan_leaves_unseen_items_retriable(self) -> None:
        fake = FakeBatchBrowser(["video00000A"], complete=False)
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

        self.assertEqual(result.removed, 1)
        self.assertEqual(result.not_found, 2)
        self.assertEqual(result.run_status, "partial")

    def test_loaded_video_ids_use_one_bulk_dom_evaluation(self) -> None:
        locator = BulkRowsLocator(
            [
                "/watch?v=video00000A&list=WL",
                None,
                "/watch?v=video00000B&list=WL",
                "/watch?v=video00000A&list=WL",
            ]
        )
        client = object.__new__(PlaywrightWatchLaterClient)
        client._page = BulkRowsPage(locator)

        self.assertEqual(client._loaded_video_ids(), ["video00000A", "video00000B"])
        self.assertEqual(locator.evaluate_all_calls, 1)
        self.assertEqual(locator.script.count("querySelector"), 1)

    def test_single_scan_yields_loaded_matches_and_advances_after_batch(self) -> None:
        client = ScriptedScanClient(
            [["video00000A", "other000001"], ["video00000B", "other000001"]],
            [_ScrollMetrics(0, 100, 1000)],
        )

        events = list(client.scan_matching_videos({"video00000A", "video00000B"}))

        self.assertEqual(
            [(event.kind, event.video_id) for event in events],
            [
                ("candidate", "video00000A"),
                ("candidate", "video00000B"),
                ("finished", None),
            ],
        )
        self.assertEqual(client.scroll_calls, 1)

    def test_stable_end_ignores_dynamic_document_height(self) -> None:
        client = ScriptedScanClient(
            [["other000001"]],
            [
                _ScrollMetrics(900, 100, 1000),
                _ScrollMetrics(901, 100, 1001),
                _ScrollMetrics(902, 100, 1002),
            ],
            stable_rounds=2,
        )

        events = list(client.scan_matching_videos({"missing0001"}))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "finished")
        self.assertTrue(events[0].complete)
        self.assertEqual(events[0].remaining_video_ids, ("missing0001",))
        self.assertEqual(client.scroll_calls, 2)

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

    def test_stale_pending_item_is_skipped_while_current_items_continue(self) -> None:
        fake = FakeBrowser(
            {
                "video00000A": "removed",
                "video00000B": "removed",
                "video00000C": "removed",
            }
        )
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Delete A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
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

        self.assertEqual(fake.calls, ["video00000B", "video00000C"])
        self.assertEqual(result.stale, 1)
        self.assertEqual(result.removed, 2)
        self.assertEqual(result.run_status, "partial")
        stale_item = next(row for row in payload["items"] if row["video_id"] == "video00000A")
        self.assertEqual(stale_item["status"], "planned")
        self.assertTrue(stale_item["stale"])

    def test_terminal_stale_checkpoint_does_not_block_resume(self) -> None:
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
            selection = select_title(conn, contains="Delete A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
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

        self.assertEqual(first.removed, 1)
        self.assertEqual(second.stale, 0)
        self.assertEqual(second.removed, 2)
        self.assertEqual(fake.calls, ["video00000A", "video00000B", "video00000C"])
        self.assertEqual(second.run_status, "complete")

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
