from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import connect
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.reports import creator_rows


class ImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalogue.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_export(self, name: str, entries: list[dict], **playlist_fields: object) -> Path:
        path = self.root / name
        document = {
            "id": "WL",
            "title": "Watch later",
            "playlist_count": len(entries),
            "entries": entries,
            **playlist_fields,
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_import_is_idempotent_and_preserves_order(self) -> None:
        source = self._write_export(
            "watch-later.json",
            [
                {
                    "id": "aaa111",
                    "url": "https://www.youtube.com/watch?v=aaa111",
                    "title": "First",
                    "channel_id": "chan-a",
                    "channel": "Creator A",
                    "duration": 60,
                    "view_count": 100,
                },
                {
                    "id": "bbb222",
                    "url": "https://www.youtube.com/watch?v=bbb222",
                    "title": "Second",
                    "channel_id": "chan-b",
                    "channel": "Creator B",
                    "duration": 120,
                    "view_count": 200,
                },
            ],
        )

        with connect(self.db_path) as conn:
            first = import_watchlater_json(conn, source)
            second = import_watchlater_json(conn, source)
            entries = conn.execute(
                "SELECT position, video_id, title FROM snapshot_entries ORDER BY position"
            ).fetchall()
            snapshot_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

        self.assertFalse(first.already_present)
        self.assertTrue(second.already_present)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(snapshot_count, 1)
        self.assertEqual(
            [(row["position"], row["video_id"], row["title"]) for row in entries],
            [(1, "aaa111", "First"), (2, "bbb222", "Second")],
        )

    def test_later_snapshot_does_not_destroy_old_metadata(self) -> None:
        first_source = self._write_export(
            "one.json",
            [{"id": "aaa111", "title": "Old title", "channel_id": "chan-a", "channel": "Creator A"}],
        )
        second_source = self._write_export(
            "two.json",
            [{"id": "aaa111", "title": "New title", "channel_id": "chan-a", "channel": "Creator A"}],
        )

        with connect(self.db_path) as conn:
            first = import_watchlater_json(conn, first_source)
            second = import_watchlater_json(conn, second_source)
            old_title = conn.execute(
                "SELECT title FROM snapshot_entries WHERE snapshot_id = ?",
                (first.snapshot_id,),
            ).fetchone()[0]
            new_title = conn.execute(
                "SELECT title FROM snapshot_entries WHERE snapshot_id = ?",
                (second.snapshot_id,),
            ).fetchone()[0]

        self.assertEqual(old_title, "Old title")
        self.assertEqual(new_title, "New title")

    def test_creator_report_groups_by_channel_id(self) -> None:
        source = self._write_export(
            "watch-later.json",
            [
                {
                    "id": "aaa111",
                    "title": "A",
                    "channel_id": "stable-channel",
                    "channel": "Creator Old Name",
                    "duration": 60,
                    "view_count": 10,
                },
                {
                    "id": "bbb222",
                    "title": "B",
                    "channel_id": "stable-channel",
                    "channel": "Creator Renamed",
                    "duration": 180,
                    "view_count": 30,
                },
                {
                    "id": "ccc333",
                    "title": "C",
                    "channel_id": "other-channel",
                    "channel": "Other Creator",
                    "duration": 30,
                    "view_count": 20,
                },
            ],
        )

        with connect(self.db_path) as conn:
            imported = import_watchlater_json(conn, source)
            rows = creator_rows(conn, imported.snapshot_id)

        self.assertEqual(rows[0].channel_id, "stable-channel")
        self.assertEqual(rows[0].count, 2)
        self.assertEqual(rows[0].total_duration, 240)
        self.assertEqual(rows[0].median_duration, 120)
        self.assertEqual(rows[0].median_views, 20)


if __name__ == "__main__":
    unittest.main()
