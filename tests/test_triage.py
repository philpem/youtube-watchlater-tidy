from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import ensure_schema, open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.reports import creator_rows, video_rows
from youtube_watchlater_tidy.triage import (
    apply_selection_action,
    select_creator,
    select_title,
    selection_rows,
    undo_selection_action,
)


class TriageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalogue.sqlite3"
        self.source = self.root / "watch-later.json"
        self.source.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "title": "Watch later",
                    "entries": [
                        {"id": "a", "title": "Power supply teardown", "channel_id": "big", "channel": "Big Clive", "duration": 600, "view_count": 1000},
                        {"id": "b", "title": "LED lamp teardown", "channel_id": "big", "channel": "Big Clive", "duration": 900, "view_count": 2000},
                        {"id": "c", "title": "Super Mario World secrets", "channel_id": "games", "channel": "Games", "duration": 300, "view_count": 3000},
                        {"id": "d", "title": "Super Mario 64 history", "channel_id": "retro", "channel": "Retro", "duration": 1200, "view_count": 4000},
                        {"id": "e", "title": "Other video", "channel_id": "other", "channel": "Other", "duration": 60, "view_count": 10},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, self.source).snapshot_id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_creator_selection_move_and_remaining_report(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_creator(conn, "big", self.snapshot, remaining=True)
            self.assertEqual(selection.entry_count, 2)
            rows = selection_rows(conn, selection.selection_id)
            self.assertEqual([row.video_id for row in rows], ["a", "b"])
            changed = apply_selection_action(
                conn,
                "move",
                destination_playlist="Queue - Electronics",
                selection_id=selection.selection_id,
            )
            self.assertEqual(changed, 2)
            big_all = next(row for row in creator_rows(conn, self.snapshot) if row.channel_id == "big")
            self.assertEqual(big_all.unresolved_count, 0)
            self.assertEqual(big_all.action_counts, {"move": 2})
            remaining_ids = {row.channel_id for row in creator_rows(conn, self.snapshot, remaining=True)}
            self.assertNotIn("big", remaining_ids)

    def test_title_contains_is_case_insensitive_and_can_be_undone(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="super mario", snapshot_id=self.snapshot, remaining=True)
            self.assertEqual(selection.entry_count, 2)
            apply_selection_action(conn, "archive", selection_id=selection.selection_id)
            self.assertEqual(len(creator_rows(conn, self.snapshot, remaining=True)), 2)
            cleared = undo_selection_action(conn, selection_id=selection.selection_id)
            self.assertEqual(cleared, 2)
            self.assertEqual(sum(row.count for row in creator_rows(conn, self.snapshot, remaining=True)), 5)
            history_count = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            self.assertEqual(history_count, 4)

    def test_title_regex_and_range_filters(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(
                conn,
                regex=r"teardown$",
                snapshot_id=self.snapshot,
                min_duration=700,
            )
            rows = selection_rows(conn, selection.selection_id)
            self.assertEqual([row.video_id for row in rows], ["b"])

    def test_creator_name_ambiguity_is_rejected(self) -> None:
        ambiguous = self.root / "ambiguous.json"
        ambiguous.write_text(json.dumps({"id": "WL", "entries": [
            {"id": "x", "title": "X", "channel_id": "one", "channel": "Same Name"},
            {"id": "y", "title": "Y", "channel_id": "two", "channel": "Same Name"},
        ]}), encoding="utf-8")
        with open_catalogue(self.db_path) as conn:
            snap = import_watchlater_json(conn, ambiguous).snapshot_id
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                select_creator(conn, "Same Name", snap)

    def test_unknown_creator_video_report(self) -> None:
        source = self.root / "unknown.json"
        source.write_text(json.dumps({"id": "WL", "entries": [
            {"id": "known", "title": "Known", "channel_id": "known-channel", "channel": "Known creator", "duration": 60},
            {"id": "unknown", "title": "Mystery video", "duration": 120, "view_count": 12345},
        ]}), encoding="utf-8")
        with open_catalogue(self.db_path) as conn:
            snap = import_watchlater_json(conn, source).snapshot_id
            rows = video_rows(conn, snap, unknown_creator=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].video_id, "unknown")
        self.assertEqual(rows[0].title, "Mystery video")
        self.assertEqual(rows[0].view_count, 12345)

    def test_schema_v1_migrates_to_v2(self) -> None:
        old = self.root / "old.sqlite3"
        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE snapshots (id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL UNIQUE, imported_at TEXT NOT NULL, playlist_id TEXT, playlist_title TEXT, playlist_modified_date TEXT, reported_playlist_count INTEGER, entry_count INTEGER NOT NULL);
            CREATE TABLE videos (video_id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
            CREATE TABLE snapshot_entries (snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), position INTEGER NOT NULL, video_id TEXT NOT NULL REFERENCES videos(video_id), title TEXT, description TEXT, channel_id TEXT, channel TEXT, uploader TEXT, uploader_id TEXT, duration REAL, view_count INTEGER, availability TEXT, timestamp REAL, release_timestamp REAL, thumbnails_json TEXT, raw_json TEXT NOT NULL, PRIMARY KEY(snapshot_id, position), UNIQUE(snapshot_id, video_id));
            PRAGMA user_version = 1;
        """)
        ensure_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(version, 2)
        self.assertIn("decision_events", tables)
        self.assertIn("selections", tables)


if __name__ == "__main__":
    unittest.main()
