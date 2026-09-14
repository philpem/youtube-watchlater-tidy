from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import ensure_schema, open_catalogue
from youtube_watchlater_tidy.enrichment import latest_found_observation
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.preservetube import PRESERVETUBE_SOURCE
from youtube_watchlater_tidy.recovery import (
    archive_links,
    candidate_unavailable_video_ids,
    latest_archive_lookup,
    recover_with_findyoutubevideo,
)
from youtube_watchlater_tidy.reports import creator_rows, video_rows
from youtube_watchlater_tidy.triage import select_title, selection_rows


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

    def _filmot_response(self) -> dict:
        return {
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
                            "url": "https://filmot.com/video/deleted",
                            "contains": "metadata",
                            "title": "Metadata",
                            "note": "historical metadata",
                        }
                    ],
                    "rawraw": [
                        {
                            "id": "deleted",
                            "title": "Recovered old title",
                            "description": "Recovered old description",
                            "channelid": "UCARCHIVE",
                            "channelname": "Recovered Channel",
                            "uploaddate": "2020-01-02T03:04:05Z",
                            "duration": 321,
                            "views": 4567,
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

    def _preservetube_response(self) -> dict:
        return {
            "id": "deleted",
            "api_version": 5,
            "keys": [
                {
                    "name": "PreserveTube",
                    "classname": "preservetube",
                    "archived": True,
                    "metaonly": False,
                    "comments": False,
                    "maybe_paywalled": False,
                    "available": [
                        {
                            "url": "https://preservetube.com/watch?v=deleted",
                            "contains": ["video", "metadata", "thumbnail"],
                            "title": "Video",
                            "note": None,
                        }
                    ],
                    # FindYouTubeVideo currently discards PreserveTube's raw JSON.
                    "rawraw": None,
                }
            ],
            "verdict": {
                "video": True,
                "metaonly": False,
                "comments": False,
                "human_friendly": "Video found",
            },
        }

    def test_found_result_preserves_links_and_normalises_filmot_metadata(self) -> None:
        response = self._filmot_response()

        with open_catalogue(self.db_path) as conn:
            result = recover_with_findyoutubevideo(
                conn,
                ["deleted"],
                fetcher=lambda _: response,
                show_progress=False,
            )
            row = latest_archive_lookup(conn, "deleted")
            metadata = latest_found_observation(conn, "deleted")
            original = conn.execute(
                "SELECT title, channel_id, channel FROM snapshot_entries "
                "WHERE snapshot_id = ? AND video_id = 'deleted'",
                (self.snapshot,),
            ).fetchone()
            videos = {row.video_id: row for row in video_rows(conn, self.snapshot)}
            creators = creator_rows(conn, self.snapshot)
            selection = select_title(
                conn,
                contains="recovered old title",
                snapshot_id=self.snapshot,
            )
            selected = selection_rows(conn, selection.selection_id)

        self.assertEqual(
            (result.found, result.not_found, result.failed, result.metadata_recovered),
            (1, 0, 0, 1),
        )
        self.assertEqual(row["status"], "found")
        self.assertEqual(row["has_video"], 1)
        self.assertEqual(row["has_metadata"], 1)
        self.assertEqual(row["human_verdict"], "Video and metadata found")
        self.assertEqual(json.loads(row["raw_json"]), response)

        self.assertEqual(metadata["source"], "filmot-via-findyoutubevideo")
        self.assertEqual(metadata["title"], "Recovered old title")
        self.assertEqual(metadata["channel_id"], "UCARCHIVE")
        self.assertEqual(metadata["channel"], "Recovered Channel")
        self.assertEqual(metadata["duration"], 321)
        self.assertEqual(metadata["view_count"], 4567)

        # Recovery enriches the effective view without mutating the source snapshot.
        self.assertEqual(original["title"], "[Deleted video]")
        self.assertIsNone(original["channel_id"])
        self.assertIsNone(original["channel"])
        self.assertEqual(videos["deleted"].title, "Recovered old title")
        self.assertEqual(videos["deleted"].creator, "Recovered Channel")
        recovered_creator = next(row for row in creators if row.channel_id == "UCARCHIVE")
        self.assertEqual(recovered_creator.name, "Recovered Channel")
        self.assertEqual([row.video_id for row in selected], ["deleted"])
        self.assertEqual(selected[0].title, "Recovered old title")

        links = archive_links(response)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].service, "Filmot")
        self.assertEqual(links[0].contains, "metadata")
        self.assertIn("video", links[1].contains)

    def test_preservetube_is_used_as_metadata_fallback(self) -> None:
        response = self._preservetube_response()
        calls: list[str] = []

        def preservetube_fetch(video_id: str) -> dict:
            calls.append(video_id)
            return {
                "id": video_id,
                "title": "Preserved old title",
                "description": "Preserved description",
                "channel": "Preserved Channel",
                "channelId": "UCPRESERVED",
                "published": "2019-05-06T07:08:09.000Z",
                "archived": "2024-01-02T03:04:05.000Z",
                "thumbnail": "https://example.invalid/thumb.jpg",
                "source": "https://example.invalid/video.mp4",
                "deletion_stage": None,
            }

        with open_catalogue(self.db_path) as conn:
            result = recover_with_findyoutubevideo(
                conn,
                ["deleted"],
                fetcher=lambda _: response,
                preservetube_fetcher=preservetube_fetch,
                show_progress=False,
            )
            metadata = latest_found_observation(conn, "deleted")
            videos = {row.video_id: row for row in video_rows(conn, self.snapshot)}
            selection = select_title(
                conn,
                contains="preserved old title",
                snapshot_id=self.snapshot,
            )
            selected = selection_rows(conn, selection.selection_id)

        self.assertEqual(calls, ["deleted"])
        self.assertEqual(result.metadata_recovered, 1)
        self.assertEqual(metadata["source"], PRESERVETUBE_SOURCE)
        self.assertEqual(metadata["title"], "Preserved old title")
        self.assertEqual(metadata["channel_id"], "UCPRESERVED")
        self.assertEqual(metadata["channel"], "Preserved Channel")
        self.assertEqual(metadata["upload_date"], "2019-05-06T07:08:09.000Z")
        self.assertEqual(videos["deleted"].title, "Preserved old title")
        self.assertEqual(videos["deleted"].creator, "Preserved Channel")
        self.assertEqual([row.video_id for row in selected], ["deleted"])

    def test_preservetube_is_not_called_when_filmot_metadata_exists(self) -> None:
        def should_not_run(_: str) -> dict:
            self.fail("PreserveTube fallback should not run when Filmot recovered metadata")

        response = self._filmot_response()
        response["keys"].append(self._preservetube_response()["keys"][0])
        with open_catalogue(self.db_path) as conn:
            result = recover_with_findyoutubevideo(
                conn,
                ["deleted"],
                fetcher=lambda _: response,
                preservetube_fetcher=should_not_run,
                show_progress=False,
            )

        self.assertEqual(result.metadata_recovered, 1)

    def test_no_filmot_raw_data_does_not_create_metadata_observation(self) -> None:
        response = self._filmot_response()
        response["keys"][0].pop("rawraw")
        with open_catalogue(self.db_path) as conn:
            result = recover_with_findyoutubevideo(
                conn,
                ["deleted"],
                fetcher=lambda _: response,
                show_progress=False,
            )
            metadata = latest_found_observation(conn, "deleted")

        self.assertEqual(result.metadata_recovered, 0)
        self.assertIsNone(metadata)

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
