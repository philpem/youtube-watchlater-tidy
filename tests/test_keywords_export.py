from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.keywords import keyword_rows
from youtube_watchlater_tidy.selection_export import selection_export_text
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class KeywordAndExportTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "Z80 repair and debugging", "channel": "Retro Lab"},
                        {"id": "video00000E", "title": "Z80 repair techniques", "channel": "Retro Lab"},
                        {"id": "video00000I", "title": "Home Assistant ESPHome sensor repair", "channel": "Automation"},
                        {"id": "video00000M", "title": "C++ on a Z80?", "channel": "Odd Machines"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_keyword_counts_are_per_video_and_keep_technical_tokens(self) -> None:
        with open_catalogue(self.db_path) as conn:
            rows = keyword_rows(conn, self.snapshot, ngram=1, min_count=1)
        counts = {row.phrase: row.count for row in rows}
        self.assertEqual(counts["z80"], 3)
        self.assertEqual(counts["repair"], 3)
        self.assertEqual(counts["c++"], 1)
        self.assertNotIn("and", counts)

    def test_bigram_discovery_and_remaining_filter(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Home Assistant", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            rows = keyword_rows(
                conn,
                self.snapshot,
                remaining=True,
                ngram=2,
                min_count=1,
            )
        phrases = {row.phrase for row in rows}
        self.assertIn("z80 repair", phrases)
        self.assertNotIn("home assistant", phrases)

    def test_selection_json_and_csv_export(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Z80", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Retrocomputing",
                selection_id=selection.selection_id,
            )
            json_text = selection_export_text(conn, selection.selection_id, format="json")
            csv_text = selection_export_text(conn, selection.selection_id, format="csv")

        payload = json.loads(json_text)
        self.assertEqual(payload["selection_id"], selection.selection_id)
        self.assertEqual(payload["selector_type"], "title")
        self.assertEqual(payload["selector"]["contains"], "Z80")
        self.assertEqual(len(payload["videos"]), 3)
        self.assertTrue(
            all(video["destination_playlist"] == "Queue - Retrocomputing" for video in payload["videos"])
        )
        self.assertIn("position,video_id,title,creator,channel_id,duration,action,destination_playlist", csv_text)
        self.assertIn("Queue - Retrocomputing", csv_text)


if __name__ == "__main__":
    unittest.main()
