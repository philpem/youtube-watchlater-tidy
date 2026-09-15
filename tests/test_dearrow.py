from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.db import ensure_schema, open_catalogue
from youtube_watchlater_tidy.dearrow import (
    candidate_video_ids,
    enrich_dearrow,
    fetch_dearrow,
    latest_lookup,
    preferred_for_video,
    preferred_title,
)
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self) -> bytes:
        return self.payload


class DeArrowTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "YOU WON'T BELIEVE THIS", "channel": "One"},
                        {"id": "video00000E", "title": "Another title", "channel": "Two"},
                        {"id": "video00000I", "title": "Third title", "channel": "Three"},
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
    def _branding(
        title: str = "A useful descriptive title",
        *,
        votes: int = 2,
        locked: bool = False,
        original: bool = False,
    ):
        return {
            "titles": [
                {
                    "title": title,
                    "original": original,
                    "votes": votes,
                    "locked": locked,
                    "UUID": "abc-123",
                },
                {
                    "title": "Rejected alternative",
                    "original": False,
                    "votes": -2,
                    "locked": False,
                    "UUID": "def-456",
                },
            ],
            "thumbnails": [],
            "randomTime": 0.2,
            "videoDuration": 100,
        }

    def test_preferred_title_uses_first_entry_and_trust_rule(self) -> None:
        trusted = self._branding()["titles"]
        self.assertEqual(preferred_title(trusted), "A useful descriptive title")
        locked = self._branding(votes=-5, locked=True)["titles"]
        self.assertEqual(preferred_title(locked), "A useful descriptive title")
        untrusted = self._branding(votes=-1, locked=False)["titles"]
        self.assertIsNone(preferred_title(untrusted))
        original = self._branding(original=True)["titles"]
        self.assertIsNone(preferred_title(original))

    def test_enrichment_preserves_all_title_fields_and_preferred_title(self) -> None:
        response = self._branding()
        with open_catalogue(self.db_path) as conn:
            result = enrich_dearrow(
                conn,
                ["video00000A"],
                fetcher=lambda _: response,
                workers=1,
                show_progress=False,
            )
            row = latest_lookup(conn, "video00000A")
            preferred = preferred_for_video(conn, "video00000A")
            original = conn.execute(
                "SELECT title FROM snapshot_entries WHERE snapshot_id = ? AND video_id = ?",
                (self.snapshot, "video00000A"),
            ).fetchone()[0]

        self.assertEqual((result.found, result.preferred, result.failed), (1, 1, 0))
        self.assertEqual(row["preferred_title"], "A useful descriptive title")
        titles = json.loads(row["titles_json"])
        self.assertEqual(titles[0]["votes"], 2)
        self.assertFalse(titles[0]["original"])
        self.assertFalse(titles[0]["locked"])
        self.assertEqual(titles[0]["UUID"], "abc-123")
        self.assertEqual(preferred, "A useful descriptive title")
        self.assertEqual(original, "YOU WON'T BELIEVE THIS")

    def test_newer_untrusted_result_revokes_stale_preferred_title(self) -> None:
        with open_catalogue(self.db_path) as conn:
            enrich_dearrow(
                conn,
                ["video00000A"],
                fetcher=lambda _: self._branding(),
                workers=1,
                show_progress=False,
            )
            self.assertEqual(preferred_for_video(conn, "video00000A"), "A useful descriptive title")
            enrich_dearrow(
                conn,
                ["video00000A"],
                fetcher=lambda _: self._branding(votes=-1),
                workers=1,
                show_progress=False,
            )
            self.assertIsNone(preferred_for_video(conn, "video00000A"))

    def test_untrusted_submission_is_cached_but_not_preferred(self) -> None:
        response = self._branding(votes=-1)
        with open_catalogue(self.db_path) as conn:
            result = enrich_dearrow(
                conn,
                ["video00000A"],
                fetcher=lambda _: response,
                workers=1,
                show_progress=False,
            )
            row = latest_lookup(conn, "video00000A")
            preferred = preferred_for_video(conn, "video00000A")
        self.assertEqual(result.found, 1)
        self.assertEqual(result.preferred, 0)
        self.assertIsNone(row["preferred_title"])
        self.assertIsNone(preferred)

    def test_candidate_cache_and_remaining_filter(self) -> None:
        with open_catalogue(self.db_path) as conn:
            enrich_dearrow(
                conn,
                ["video00000A"],
                fetcher=lambda _: self._branding(),
                workers=1,
                show_progress=False,
            )
            selection = select_title(conn, contains="Third", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)

            self.assertEqual(
                candidate_video_ids(conn, self.snapshot, all_videos=True, limit=10),
                ["video00000E", "video00000I"],
            )
            self.assertEqual(
                candidate_video_ids(
                    conn,
                    self.snapshot,
                    all_videos=True,
                    remaining=True,
                    limit=10,
                ),
                ["video00000E"],
            )
            self.assertEqual(
                candidate_video_ids(
                    conn,
                    self.snapshot,
                    video_ids=["video00000A"],
                    refresh=True,
                ),
                ["video00000A"],
            )

    def test_hash_prefix_mode_extracts_only_requested_video(self) -> None:
        video_id = "video00000A"
        prefix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]
        payload = {
            video_id: self._branding(),
            "other000000E": self._branding("Other result"),
        }
        with patch(
            "youtube_watchlater_tidy.dearrow.urlopen",
            return_value=_FakeResponse(payload),
        ) as mocked:
            result = fetch_dearrow(video_id, hash_prefix=True)
        request = mocked.call_args.args[0]
        self.assertIn(f"/api/branding/{prefix}", request.full_url)
        self.assertEqual(result["titles"][0]["title"], "A useful descriptive title")

    def test_hash_prefix_mode_is_recorded_in_source_url(self) -> None:
        with open_catalogue(self.db_path) as conn:
            enrich_dearrow(
                conn,
                ["video00000A"],
                fetcher=lambda _: self._branding(),
                hash_prefix=True,
                workers=1,
                show_progress=False,
            )
            row = latest_lookup(conn, "video00000A")
        prefix = hashlib.sha256(b"video00000A").hexdigest()[:4]
        self.assertIn(f"/api/branding/{prefix}", row["source_url"])
        self.assertNotIn("video00000A", row["source_url"])

    def test_schema_v5_migrates_to_v6(self) -> None:
        path = Path(self.tmp.name) / "v5.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA user_version = 5")
        ensure_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        self.assertEqual(version, 6)
        self.assertIn("dearrow_lookups", objects)
        self.assertIn("preferred_dearrow", objects)


if __name__ == "__main__":
    unittest.main()
