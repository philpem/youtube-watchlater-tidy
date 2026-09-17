from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_classification import (
    ClassificationBatchResult,
    ClassificationEvidence,
    ClassificationRunResult,
    ClassificationSuggestion,
)
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.llm_prompt import RenderedPrompt
from youtube_watchlater_tidy.llm_store import store_run
from youtube_watchlater_tidy.review_report import (
    REVIEW_FORMAT,
    apply_review_decisions,
    load_review_decisions,
    render_review_html,
    review_rows,
)
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class ReviewReportTests(unittest.TestCase):
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
                            "id": "video00000A",
                            "title": "Original A",
                            "channel_id": "UCONE",
                            "channel": "One",
                            "duration": 60,
                            "view_count": 100,
                            "thumbnails": [{"url": "https://example.invalid/a.jpg", "width": 320, "height": 180}],
                        },
                        {
                            "id": "video00000E",
                            "title": "Original B",
                            "channel_id": "UCTWO",
                            "channel": "Two",
                            "duration": 120,
                            "view_count": 200,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        provider = ProviderConfig(
            name="test",
            preset="generic",
            base_url="https://example.invalid/v1",
            model="model-a",
        )
        prompt = RenderedPrompt(
            system="system",
            interest_brief="interests",
            playlist_guidance="",
            profile_name="default",
            sha256="prompt-hash",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            conn.execute(
                """
                INSERT INTO dearrow_lookups (
                    video_id, looked_up_at, status, preferred_title,
                    titles_json, source_url, raw_json
                ) VALUES ('video00000A', '2026-09-15T12:00:00+00:00', 'found',
                          'Descriptive A', '[]', NULL, '{}')
                """
            )
            archive_raw = {
                "keys": [
                    {
                        "name": "Filmot",
                        "available": [
                            {
                                "url": "https://example.invalid/metadata/video00000A",
                                "contains": "metadata",
                                "title": "Metadata",
                            }
                        ],
                    },
                    {
                        "name": "GhostArchive",
                        "available": [
                            {
                                "url": "https://example.invalid/archive/video00000A",
                                "contains": ["video", "metadata"],
                                "title": "Archived video",
                            }
                        ],
                    },
                ],
                "verdict": {"video": True, "metaonly": True, "comments": False},
            }
            conn.execute(
                """
                INSERT INTO archive_lookups (
                    video_id, backend, looked_up_at, status,
                    has_video, has_metadata, has_comments,
                    human_verdict, source_url, raw_json
                ) VALUES (?, 'findyoutubevideo-v5', '2026-09-15T12:01:00+00:00',
                          'found', 1, 1, 0, 'Video found',
                          'https://findyoutubevideo.thetechrobo.ca/?q=video00000A', ?)
                """,
                ("video00000A", json.dumps(archive_raw)),
            )
            selection = select_title(conn, contains="Original B", snapshot_id=self.snapshot)
            apply_selection_action(
                conn,
                "archive",
                selection_id=selection.selection_id,
                reason="manual archive",
            )
            videos = [
                ClassificationEvidence(
                    video_id="video00000A",
                    playlist_position=1,
                    original_title="Original A",
                    recovered_title=None,
                    recovered_source=None,
                    dearrow_title="Descriptive A",
                    channel="One",
                    channel_id="UCONE",
                    duration=60,
                    view_count=100,
                    upload_date=None,
                    availability=None,
                ),
                ClassificationEvidence(
                    video_id="video00000E",
                    playlist_position=2,
                    original_title="Original B",
                    recovered_title=None,
                    recovered_source=None,
                    dearrow_title=None,
                    channel="Two",
                    channel_id="UCTWO",
                    duration=120,
                    view_count=200,
                    upload_date=None,
                    availability=None,
                ),
            ]
            suggestions = tuple(self._suggestion(v.video_id) for v in videos)
            batch = ClassificationBatchResult(
                suggestions=suggestions,
                input_sha256="batch",
                usage={},
                response_model="model-a",
            )
            self.run_id = store_run(
                conn,
                snapshot_id=self.snapshot,
                provider=provider,
                prompt=prompt,
                videos=videos,
                result=ClassificationRunResult(suggestions=suggestions, batches=(batch,)),
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _suggestion(video_id: str) -> ClassificationSuggestion:
        return ClassificationSuggestion(
            video_id=video_id,
            action="review",
            topic="electronics",
            content_type="technical",
            timeliness="evergreen",
            quality=0.8,
            confidence=0.65,
            reason="Worth checking manually.",
            existing_playlist=None,
            new_queue_proposal=None,
            destination_confidence=0.0,
            destination_reason="",
            needs_description=False,
            needs_transcript=False,
        )

    def test_report_keeps_current_decision_and_llm_separate(self) -> None:
        with open_catalogue(self.db_path) as conn:
            snapshot, rows = review_rows(conn, self.snapshot)
        self.assertEqual(snapshot, self.snapshot)
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["dearrow_title"], "Descriptive A")
        self.assertIsNone(first["current_decision"])
        self.assertEqual(first["llm"]["run_id"], self.run_id)
        self.assertEqual(first["llm"]["confidence"], 0.65)
        self.assertEqual(first["thumbnail"], "https://example.invalid/a.jpg")
        self.assertEqual(len(first["recovered_video_links"]), 1)
        self.assertEqual(first["recovered_video_links"][0]["service"], "GhostArchive")
        self.assertEqual(first["recovered_video_links"][0]["url"], "https://example.invalid/archive/video00000A")
        self.assertEqual(second["current_decision"]["action"], "archive")
        self.assertEqual(second["current_decision"]["reason"], "manual archive")
        self.assertEqual(second["llm"]["action"], "review")

    def test_html_is_self_contained_and_exports_review_format(self) -> None:
        with open_catalogue(self.db_path) as conn:
            snapshot, rows = review_rows(conn, self.snapshot)
        page = render_review_html(snapshot, rows)
        self.assertIn("<!doctype html>", page.lower())
        self.assertIn("Original A", page)
        self.assertIn("Descriptive A", page)
        self.assertIn(REVIEW_FORMAT, page)
        self.assertIn("Export explicit overrides", page)
        self.assertIn("human-review-report", page)
        self.assertIn("Recovered video:", page)
        self.assertIn("https://example.invalid/archive/video00000A", page)
        self.assertNotIn("https://example.invalid/metadata/video00000A", page)
        self.assertIn("JSON.stringify(payload,null,2)+\'\\n\'", page)
        self.assertNotIn("JSON.stringify(payload,null,2)+\'" + chr(10) + "\'", page)

    def test_dry_run_and_import_are_safe_and_idempotent(self) -> None:
        payload = {
            "format": REVIEW_FORMAT,
            "snapshot_id": self.snapshot,
            "created_at": "2026-09-15T12:00:00Z",
            "decisions": [
                {"video_id": "video00000A", "action": "keep", "note": "good reference"}
            ],
        }
        path = self.root / "decisions.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_review_decisions(path)

        with open_catalogue(self.db_path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            dry = apply_review_decisions(conn, loaded, dry_run=True)
            after_dry = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            applied = apply_review_decisions(conn, loaded)
            after_apply = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            repeated = apply_review_decisions(conn, loaded)
            after_repeat = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            current = conn.execute(
                "SELECT action, source, reason FROM current_decisions "
                "WHERE snapshot_id = ? AND video_id = 'video00000A'",
                (self.snapshot,),
            ).fetchone()

        self.assertEqual(dry.changed, 1)
        self.assertEqual(before, after_dry)
        self.assertEqual(applied.changed, 1)
        self.assertEqual(after_apply, before + 1)
        self.assertEqual(repeated.changed, 0)
        self.assertEqual(repeated.unchanged, 1)
        self.assertEqual(after_repeat, after_apply)
        self.assertEqual(current["action"], "keep")
        self.assertEqual(current["source"], "human-review-report")
        self.assertEqual(current["reason"], "good reference")

    def test_import_rejects_video_from_another_snapshot(self) -> None:
        payload = {
            "format": REVIEW_FORMAT,
            "snapshot_id": self.snapshot,
            "decisions": [{"video_id": "not-present", "action": "delete", "note": None}],
        }
        with open_catalogue(self.db_path) as conn:
            with self.assertRaisesRegex(ValueError, "not in snapshot"):
                apply_review_decisions(conn, payload)


if __name__ == "__main__":
    unittest.main()
