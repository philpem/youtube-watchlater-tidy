from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import ensure_schema, open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.recovery import (
    archive_links,
    candidate_unavailable_video_ids,
    latest_archive_lookup,
    recover_with_findyoutubevideo,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalogue.sqlite3"
        source = self.root / "watch-later.json"
        source.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "title": "Watch later",
                    "entries": [
                        {"id": "live", "title": "Still live"},
                        {"id": "private", "title": "[Private video]"},
                        {"id": "deleted", "title": "[Deleted video]"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unavailable_candidates_and_cache(self) -> None:
        with open_catalogue(self.db_path) as conn:
            self.assertEqual(
                candidate_unavailable_video_ids(conn, self.snapshot),
                ["private", "deleted"],
            )
            recover_with_findyoutubevideo(
                conn,
                ["private"],
                fetcher=lambda video_id: {
                    "id": video_id,
                    "api_version": 5,
                    "keys": [],
                    "verdict": {
                        "video": False,
                        "metaonly": False,
                        "comments": False,
                        "human_friendly": "Nothing found",
                    },
                },
                show_progress=False,
            )
            self.assertEqual(
                candidate_unavailable_video_ids(conn, self.snapshot),
                ["deleted"],
            )
            self.assertEqual(
                candidate_unavailable_video_ids(conn, self.snapshot, refresh=True),
                ["private", "deleted"],
            )

    def test_found_result_preserves_links_and_verdict(self) -> None:
        response = {
            "id": "deleted",
            "api_version": 5,
            "keys": [
                {
                    "name": "Filmot",
                    "classname": "filmot",
                    "archived": True,
                    "metaonly": True,
                    "comments": False,
                    "maybe_paywalled": False,
                    "available": [
                        {
                            "url": "https://example.invalid/filmot/deleted",
                            "contains": "metadata",
                            "title": "Metadata",
                            "note": "historical metadata",
                        }
                    ],
                },
                {
                    "name": "GhostArchive",
                    "classname": "ghostarchive",
                    "archived": True,
                    "metaonly": False,
                    "comments": False,
                    "maybe_paywalled": False,
                    "available": [
                        {
                            "url": "https://example.invalid/ghost/deleted",
                            "contains": ["video", "metadata"],
                            "title": "Archived video",
                            "note": None,
                        }
                    ],
                },
            ],
            "verdict": {
                "video": True,
                "metaonly": True,
                "comments": False,
                "human_friendly": "Video and metadata found",
            },
        }

        with open_catalogue(self.db_path) as conn:
            result = recover_with_findyoutubevideo(
                conn,
                ["deleted"],
                fetcher=lambda _: response,
                show_progress=False,
            )
            row = latest_archive_lookup(conn, "deleted")

        self.assertEqual((result.found, result.not_found, result.failed), (1, 0, 0))
        self.assertEqual(row["status"], "found")
        self.assertEqual(row["has_video"], 1)
        self.assertEqual(row["has_metadata"], 1)
        self.assertEqual(row["human_verdict"], "Video and metadata found")
        self.assertEqual(json.loads(row["raw_json"]), response)

        links = archive_links(response)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].service, "Filmot")
        self.assertEqual(links[0].contains, "metadata")
        self.assertIn("video", links[1].contains)

    def test_wrong_video_id_is_recorded_as_error(self) -> None:
        with open_catalogue(self.db_path) as conn:
            result = recover_with_findyoutubevideo(
                conn,
                ["deleted"],
                fetcher=lambda _: {
                    "id": "someone-else",
                    "keys": [],
                    "verdict": {},
                },
                show_progress=False,
            )
            row = latest_archive_lookup(conn, "deleted")

        self.assertEqual(result.failed, 1)
        self.assertEqual(row["status"], "error")
        self.assertIn("someone-else", json.loads(row["raw_json"])["error"])

    def test_specific_video_must_belong_to_snapshot(self) -> None:
        with open_catalogue(self.db_path) as conn:
            with self.assertRaisesRegex(ValueError, "not present"):
                candidate_unavailable_video_ids(
                    conn,
                    self.snapshot,
                    video_id="not-here",
                )

    def test_schema_v3_migrates_to_v4(self) -> None:
        path = self.root / "v3.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA user_version = 3")
        ensure_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        self.assertEqual(version, 4)
        self.assertIn("archive_lookups", tables)


if __name__ == "__main__":
    unittest.main()
