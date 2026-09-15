from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import SCHEMA_VERSION, ensure_schema, open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.rules import (
    apply_enabled_rules,
    apply_rule,
    get_rule,
    save_rule_from_selection,
    set_rule_enabled,
)
from youtube_watchlater_tidy.triage import apply_selection_action, select_creator, select_title


class SavedRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalogue.sqlite3"

        first = self.root / "first.json"
        first.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "entries": [
                        {"id": "old0000000A", "title": "Old repair", "channel_id": "UC-RETRO", "channel": "Retro Lab"},
                        {"id": "old0000000E", "title": "Other", "channel_id": "UC-OTHER", "channel": "Other"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        second = self.root / "second.json"
        second.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "entries": [
                        {"id": "new0000000A", "title": "New repair", "channel_id": "UC-RETRO", "channel": "Retro Lab"},
                        {"id": "new0000000E", "title": "Z80 repair guide", "channel_id": "UC-OTHER", "channel": "Other"},
                        {"id": "new0000000I", "title": "Z80 repair advanced", "channel_id": "UC-THIRD", "channel": "Third"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.first_snapshot = import_watchlater_json(conn, first).snapshot_id
            self.second_snapshot = import_watchlater_json(conn, second).snapshot_id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_creator_rule_replays_on_future_snapshot(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_creator(conn, "UC-RETRO", snapshot_id=self.first_snapshot)
            rule_id = save_rule_from_selection(
                conn,
                selection.selection_id,
                name="retro queue",
                action="move",
                destination_playlist="Queue - Retrocomputing",
            )
            result = apply_rule(conn, rule_id, self.second_snapshot)
            current = conn.execute(
                """
                SELECT video_id, action, destination_playlist, source, rule_json
                FROM current_decisions
                WHERE snapshot_id = ?
                ORDER BY video_id
                """,
                (self.second_snapshot,),
            ).fetchall()

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.applied, 1)
        self.assertEqual(current[0]["video_id"], "new0000000A")
        self.assertEqual(current[0]["action"], "move")
        self.assertEqual(current[0]["destination_playlist"], "Queue - Retrocomputing")
        self.assertEqual(current[0]["source"], "rule")
        self.assertEqual(json.loads(current[0]["rule_json"])["rule_id"], rule_id)

    def test_rule_does_not_overwrite_human_decision(self) -> None:
        with open_catalogue(self.db_path) as conn:
            source_selection = select_title(conn, contains="Z80", snapshot_id=self.first_snapshot)
            rule_id = save_rule_from_selection(
                conn,
                source_selection.selection_id,
                name="z80 archive",
                action="archive",
            )

            human = select_title(conn, contains="advanced", snapshot_id=self.second_snapshot)
            apply_selection_action(conn, "keep", selection_id=human.selection_id)

            result = apply_rule(conn, rule_id, self.second_snapshot)
            decisions = {
                row["video_id"]: (row["action"], row["source"])
                for row in conn.execute(
                    "SELECT video_id, action, source FROM current_decisions WHERE snapshot_id = ?",
                    (self.second_snapshot,),
                )
            }

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.applied, 1)
        self.assertEqual(decisions["new0000000E"], ("archive", "rule"))
        self.assertEqual(decisions["new0000000I"], ("keep", "human"))

    def test_priority_order_claims_overlapping_items(self) -> None:
        with open_catalogue(self.db_path) as conn:
            creator_selection = select_creator(conn, "UC-OTHER", snapshot_id=self.first_snapshot)
            creator_rule = save_rule_from_selection(
                conn,
                creator_selection.selection_id,
                name="other keep",
                action="keep",
                priority=10,
            )
            title_selection = select_title(conn, contains="Other", snapshot_id=self.first_snapshot)
            title_rule = save_rule_from_selection(
                conn,
                title_selection.selection_id,
                name="other delete",
                action="delete",
                priority=20,
            )

            results = apply_enabled_rules(conn, self.first_snapshot)
            current = conn.execute(
                "SELECT action, source FROM current_decisions WHERE snapshot_id = ? AND video_id = 'old0000000E'",
                (self.first_snapshot,),
            ).fetchone()

        self.assertEqual([result.rule_id for result in results], [creator_rule, title_rule])
        self.assertEqual(current["action"], "keep")
        self.assertEqual(current["source"], "rule")

    def test_disabled_rule_is_not_applied(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_creator(conn, "UC-RETRO", snapshot_id=self.first_snapshot)
            rule_id = save_rule_from_selection(
                conn,
                selection.selection_id,
                name="disabled",
                action="archive",
            )
            set_rule_enabled(conn, rule_id, False)
            self.assertFalse(get_rule(conn, rule_id).enabled)
            results = apply_enabled_rules(conn, self.second_snapshot)
        self.assertEqual(results, [])

    def test_schema_v4_migrates_to_current_version(self) -> None:
        path = self.root / "v4.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA user_version = 4")
        ensure_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("saved_rules", tables)
        self.assertIn("dearrow_lookups", tables)


if __name__ == "__main__":
    unittest.main()
