from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.enrichment import latest_found_observation
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.recovery import recover_with_findyoutubevideo
from youtube_watchlater_tidy.reports import video_rows
from youtube_watchlater_tidy.triage import select_title, selection_rows
from youtube_watchlater_tidy.wayback import WAYBACK_SOURCE, normalise_wayback, wayback_watch_url


class WaybackRecoveryTests(unittest.TestCase):
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

    @staticmethod
    def _finder_response() -> dict:
        return {
            "id": "deleted",
            "api_version": 5,
            "keys": [
                {
                    "name": "Wayback Machine",
                    "classname": "WaybackMachine",
                    "archived": True,
                    "metaonly": True,
                    "comments": False,
                    "available": [
                        {
                            "url": "https://web.archive.org/web/20190506070809/https://www.youtube.com/watch?v=deleted",
                            "contains": {"metadata": True},
                            "title": "Watch page (may not work)",
                            "note": None,
                        }
                    ],
                }
            ],
            "verdict": {
                "video": False,
                "metaonly": True,
                "comments": False,
                "human_friendly": "Metadata found",
            },
        }

    def test_wayback_watch_url_prefers_metadata_watch_page(self) -> None:
        self.assertEqual(
            wayback_watch_url(self._finder_response()),
            "https://web.archive.org/web/20190506070809/https://www.youtube.com/watch?v=deleted",
        )

    def test_player_response_metadata_is_normalised(self) -> None:
        html = """
        <html><head><meta itemprop="datePublished" content="2018-03-04"></head><body>
        <script>
        var ytInitialPlayerResponse = {
          "videoDetails": {
            "videoId": "deleted",
            "title": "Recovered from Wayback",
            "shortDescription": "Archived description",
            "author": "Archived Channel",
            "channelId": "UCWAYBACK",
            "lengthSeconds": "615",
            "viewCount": "12345"
          }
        };
        </script></body></html>
        """
        result = normalise_wayback(
            "deleted",
            html,
            "https://web.archive.org/web/20190506070809/https://www.youtube.com/watch?v=deleted",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["title"], "Recovered from Wayback")
        self.assertEqual(result["channel_id"], "UCWAYBACK")
        self.assertEqual(result["channel"], "Archived Channel")
        self.assertEqual(result["duration"], 615.0)
        self.assertEqual(result["view_count"], 12345)
        self.assertEqual(result["upload_date"], "2018-03-04")

    def test_meta_tags_are_used_when_player_json_is_unavailable(self) -> None:
        html = """
        <html><head>
          <meta property="og:title" content="Old Meta Title">
          <meta property="og:description" content="Old description">
          <meta itemprop="channelId" content="UCMETA">
          <meta itemprop="datePublished" content="2016-07-08">
        </head><body>
          <span itemprop="author"><meta itemprop="name" content="Meta Channel"></span>
        </body></html>
        """
        result = normalise_wayback("deleted", html, "https://web.archive.org/example")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["title"], "Old Meta Title")
        self.assertEqual(result["description"], "Old description")
        self.assertEqual(result["channel_id"], "UCMETA")
        self.assertEqual(result["channel"], "Meta Channel")

    def test_wayback_is_used_after_other_metadata_sources_fail(self) -> None:
        html = """
        <html><head><meta itemprop="datePublished" content="2018-03-04"></head><body>
        <script>ytInitialPlayerResponse = {
          "videoDetails": {
            "title": "Recovered from Wayback",
            "shortDescription": "Archived description",
            "author": "Archived Channel",
            "channelId": "UCWAYBACK",
            "lengthSeconds": "615",
            "viewCount": "12345"
          }
        };</script>
        </body></html>
        """
        calls: list[str] = []

        def fetch_wayback(url: str) -> str:
            calls.append(url)
            return html

        with open_catalogue(self.db_path) as conn:
            result = recover_with_findyoutubevideo(
                conn,
                ["deleted"],
                fetcher=lambda _: self._finder_response(),
                wayback_fetcher=fetch_wayback,
                show_progress=False,
            )
            observation = latest_found_observation(conn, "deleted")
            original = conn.execute(
                "SELECT title FROM snapshot_entries WHERE snapshot_id = ? AND video_id = 'deleted'",
                (self.snapshot,),
            ).fetchone()
            videos = video_rows(conn, self.snapshot)
            selection = select_title(
                conn,
                contains="recovered from wayback",
                snapshot_id=self.snapshot,
            )
            selected = selection_rows(conn, selection.selection_id)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.metadata_recovered, 1)
        self.assertEqual(observation["source"], WAYBACK_SOURCE)
        self.assertEqual(observation["title"], "Recovered from Wayback")
        self.assertEqual(observation["channel_id"], "UCWAYBACK")
        self.assertEqual(original["title"], "[Deleted video]")
        self.assertEqual(videos[0].title, "Recovered from Wayback")
        self.assertEqual(videos[0].creator, "Archived Channel")
        self.assertEqual([row.video_id for row in selected], ["deleted"])


if __name__ == "__main__":
    unittest.main()
