from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.enrichment import (
    candidate_video_ids,
    enrich_with_ytdlp,
    latest_found_observation,
)
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.reports import creator_rows, video_rows


class EnrichmentTests(unittest.TestCase):
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
                        {
                            "id": "live",
                            "title": "Useful video",
                            "duration": 60,
                            "view_count": 100,
                        },
                        {"id": "private", "title": "[Private video]"},
                        {
                            "id": "known",
                            "title": "Known",
                            "channel_id": "known-channel",
                            "channel": "Known creator",
                            "duration": 30,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_creator_candidates_exclude_unavailable(self) -> None:
        with open_catalogue(self.db_path) as conn:
            ids = candidate_video_ids(conn, self.snapshot, missing_creator=True)
        self.assertEqual(ids, ["live"])

    def test_ytdlp_enrichment_is_stored_separately_and_used_by_reports(self) -> None:
        def fake_fetch(video_id: str) -> dict:
            self.assertEqual(video_id, "live")
            return {
                "id": "live",
                "title": "Useful video",
                "channel_id": "UC123",
                "channel": "Recovered Creator",
                "uploader": "Recovered Creator",
                "uploader_id": "@recovered",
                "description": "Longer description",
                "duration": 61,
                "view_count": 123,
                "upload_date": "20200102",
                "webpage_url": "https://www.youtube.com/watch?v=live",
            }

        with open_catalogue(self.db_path) as conn:
            result = enrich_with_ytdlp(conn, ["live"], fetcher=fake_fetch)
            observation = latest_found_observation(conn, "live")
            original = conn.execute(
                """
                SELECT channel_id, channel, description
                FROM snapshot_entries
                WHERE snapshot_id = ? AND video_id = 'live'
                """,
                (self.snapshot,),
            ).fetchone()
            unknown_ids = {
                row.video_id
                for row in video_rows(conn, self.snapshot, unknown_creator=True)
            }
            creators = creator_rows(conn, self.snapshot)

        self.assertEqual((result.attempted, result.found, result.failed), (1, 1, 0))
        self.assertEqual(observation["channel_id"], "UC123")
        self.assertEqual(observation["channel"], "Recovered Creator")
        self.assertIsNone(original["channel_id"])
        self.assertIsNone(original["channel"])
        self.assertIsNone(original["description"])
        self.assertNotIn("live", unknown_ids)
        self.assertIn("private", unknown_ids)
        recovered = next(row for row in creators if row.channel_id == "UC123")
        self.assertEqual(recovered.name, "Recovered Creator")

    def test_successful_observation_is_cached_unless_refresh_requested(self) -> None:
        with open_catalogue(self.db_path) as conn:
            enrich_with_ytdlp(
                conn,
                ["live"],
                fetcher=lambda _: {"id": "live", "channel_id": "UC123"},
            )
            cached = candidate_video_ids(
                conn,
                self.snapshot,
                missing_creator=True,
            )
            refreshed = candidate_video_ids(
                conn,
                self.snapshot,
                missing_creator=True,
                refresh=True,
            )
        self.assertEqual(cached, [])
        self.assertEqual(refreshed, ["live"])

    def test_failed_lookup_is_recorded_without_aborting_batch(self) -> None:
        def fail(_: str) -> dict:
            raise RuntimeError("video unavailable")

        with open_catalogue(self.db_path) as conn:
            result = enrich_with_ytdlp(conn, ["live"], fetcher=fail)
            row = conn.execute(
                "SELECT status, raw_json FROM metadata_observations ORDER BY id DESC LIMIT 1"
            ).fetchone()

        self.assertEqual(result.failed, 1)
        self.assertEqual(row["status"], "error")
        self.assertIn("video unavailable", json.loads(row["raw_json"])["error"])


if __name__ == "__main__":
    unittest.main()
