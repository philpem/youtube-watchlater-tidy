from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.recovery import BACKEND_NAME
from youtube_watchlater_tidy.recovery_targets import recovery_candidate_video_ids
from youtube_watchlater_tidy.triage import select_title


class RecoveryTargetTests(unittest.TestCase):
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
                        {"id": "live0000000", "title": "Still live"},
                        {"id": "delete00000A", "title": "[Deleted video]"},
                        {"id": "private0000E", "title": "[Private video]"},
                        {"id": "delete00000I", "title": "[Deleted video]"},
                        {"id": "private0000M", "title": "[Private video]"},
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
    def _cache(conn, video_id: str) -> None:
        conn.execute(
            """
            INSERT INTO archive_lookups (
                video_id, backend, looked_up_at, status,
                has_video, has_metadata, has_comments,
                human_verdict, source_url, raw_json
            ) VALUES (?, ?, '2026-09-14T00:00:00+00:00', 'found', 1, 1, 0, '-', '-', '{}')
            """,
            (video_id, BACKEND_NAME),
        )
        conn.commit()

    def test_incremental_unavailable_skips_cached_before_limit(self) -> None:
        with open_catalogue(self.db_path) as conn:
            self._cache(conn, "delete00000A")
            self._cache(conn, "private0000E")
            self.assertEqual(
                recovery_candidate_video_ids(
                    conn,
                    self.snapshot,
                    unavailable=True,
                    limit=2,
                ),
                ["delete00000I", "private0000M"],
            )

    def test_refresh_position_slice_is_stable_even_when_cached(self) -> None:
        with open_catalogue(self.db_path) as conn:
            self._cache(conn, "private0000E")
            self._cache(conn, "delete00000I")
            self.assertEqual(
                recovery_candidate_video_ids(
                    conn,
                    self.snapshot,
                    unavailable=True,
                    min_position=3,
                    max_position=4,
                    refresh=True,
                ),
                ["private0000E", "delete00000I"],
            )

    def test_repeated_exact_ids_can_be_refreshed(self) -> None:
        with open_catalogue(self.db_path) as conn:
            self._cache(conn, "delete00000A")
            self.assertEqual(
                recovery_candidate_video_ids(
                    conn,
                    self.snapshot,
                    video_ids=["delete00000A", "live0000000"],
                    refresh=True,
                ),
                ["live0000000", "delete00000A"],
            )

    def test_selection_uses_its_snapshot_and_only_unavailable_members(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(
                conn,
                regex=r".*",
                snapshot_id=self.snapshot,
            )
            self.assertEqual(
                recovery_candidate_video_ids(
                    conn,
                    selection_id=selection.selection_id,
                    refresh=True,
                ),
                ["delete00000A", "private0000E", "delete00000I", "private0000M"],
            )

    def test_position_bounds_require_unavailable_target(self) -> None:
        with open_catalogue(self.db_path) as conn:
            with self.assertRaisesRegex(ValueError, "only valid with --unavailable"):
                recovery_candidate_video_ids(
                    conn,
                    self.snapshot,
                    video_ids=["delete00000A"],
                    min_position=2,
                    refresh=True,
                )


if __name__ == "__main__":
    unittest.main()
