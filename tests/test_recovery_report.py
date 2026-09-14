from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.enrichment import store_observation
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.reports import render_videos, video_rows


class RecoveryStatusReportTests(unittest.TestCase):
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
                        {
                            "id": "live",
                            "title": "Still live",
                            "channel_id": "UCLIVE",
                            "channel": "Live Channel",
                        },
                        {"id": "deleted", "title": "[Deleted video]"},
                        {"id": "private", "title": "[Private video]"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            store_observation(
                conn,
                "deleted",
                "fixture-archive",
                "found",
                {
                    "id": "deleted",
                    "title": "Recovered title",
                    "channel_id": "UCRECOVERED",
                    "channel": "Recovered Channel",
                },
                source_url="https://example.invalid/deleted",
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unavailable_report_uses_source_marker_but_shows_effective_metadata(self) -> None:
        with open_catalogue(self.db_path) as conn:
            rows = video_rows(conn, self.snapshot, unavailable=True)
            source_title = conn.execute(
                "SELECT title FROM snapshot_entries WHERE snapshot_id = ? AND video_id = 'deleted'",
                (self.snapshot,),
            ).fetchone()[0]

        self.assertEqual([row.video_id for row in rows], ["deleted", "private"])
        self.assertEqual(rows[0].title, "Recovered title")
        self.assertEqual(rows[0].creator, "Recovered Channel")
        self.assertEqual(rows[0].metadata_source, "fixture-archive")
        self.assertEqual(rows[1].title, "[Private video]")
        self.assertIsNone(rows[1].metadata_source)
        self.assertEqual(source_title, "[Deleted video]")

    def test_recovered_and_unrecovered_filters_partition_unavailable_entries(self) -> None:
        with open_catalogue(self.db_path) as conn:
            recovered = video_rows(
                conn,
                self.snapshot,
                unavailable=True,
                recovered=True,
            )
            unrecovered = video_rows(
                conn,
                self.snapshot,
                unavailable=True,
                recovered=False,
            )

        self.assertEqual([row.video_id for row in recovered], ["deleted"])
        self.assertEqual([row.video_id for row in unrecovered], ["private"])

    def test_render_includes_metadata_provenance(self) -> None:
        with open_catalogue(self.db_path) as conn:
            rows = video_rows(conn, self.snapshot, unavailable=True)
        rendered = render_videos(rows)
        self.assertIn("Metadata", rendered)
        self.assertIn("fixture-archive", rendered)
        self.assertIn("Recovered title", rendered)


if __name__ == "__main__":
    unittest.main()
