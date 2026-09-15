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
from youtube_watchlater_tidy.llm_store import run_payload, store_run
from youtube_watchlater_tidy.llm_transcript_refinement import (
    transcript_excerpt,
    transcript_refinement_evidence,
    transcript_refinement_prompt,
)
from youtube_watchlater_tidy.transcripts import ensure_transcript_schema
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class LLMTranscriptRefinementTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "Mystery A", "channel": "One"},
                        {"id": "video00000E", "title": "Mystery B", "channel": "Two"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.provider = ProviderConfig(
            name="test",
            preset="generic",
            base_url="https://example.invalid/v1",
            model="model-a",
        )
        self.prompt = RenderedPrompt(
            system="system",
            interest_brief="interests",
            playlist_guidance="",
            profile_name="default",
            sha256="base-prompt-hash",
        )

        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            videos = [
                ClassificationEvidence(
                    video_id="video00000A",
                    playlist_position=1,
                    original_title="Mystery A",
                    recovered_title=None,
                    recovered_source=None,
                    dearrow_title=None,
                    channel="One",
                    channel_id=None,
                    duration=None,
                    view_count=None,
                    upload_date=None,
                    availability=None,
                ),
                ClassificationEvidence(
                    video_id="video00000E",
                    playlist_position=2,
                    original_title="Mystery B",
                    recovered_title=None,
                    recovered_source=None,
                    dearrow_title=None,
                    channel="Two",
                    channel_id=None,
                    duration=None,
                    view_count=None,
                    upload_date=None,
                    availability=None,
                ),
            ]
            suggestions = tuple(self._suggestion(video.video_id, True) for video in videos)
            batch = ClassificationBatchResult(
                suggestions=suggestions,
                input_sha256="parent-batch",
                usage={},
                response_model="model-a",
            )
            self.parent_run = store_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                result=ClassificationRunResult(suggestions=suggestions, batches=(batch,)),
            )
            ensure_transcript_schema(conn)
            text = "BEGIN " + ("A" * 120) + " MIDDLE " + ("B" * 120) + " END"
            conn.execute(
                """
                INSERT INTO transcript_observations (
                    video_id, fetched_at, request_key, status,
                    source_type, language, language_name, format,
                    transcript_text, segments_json, source_url,
                    metadata_json, raw_text
                ) VALUES (?, ?, ?, 'found', 'manual', 'en', 'English', 'json3', ?, '[]', ?, '{}', ?)
                """,
                (
                    "video00000A",
                    "2026-09-15T12:00:00+00:00",
                    "request-key",
                    text,
                    "https://example.invalid/caption",
                    text,
                ),
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _suggestion(video_id: str, needs_transcript: bool) -> ClassificationSuggestion:
        return ClassificationSuggestion(
            video_id=video_id,
            action="review",
            topic="unknown",
            content_type="unknown",
            timeliness="unknown",
            quality=0.5,
            confidence=0.4,
            reason="Need spoken-content evidence.",
            existing_playlist=None,
            new_queue_proposal=None,
            destination_confidence=0.0,
            destination_reason="",
            needs_description=False,
            needs_transcript=needs_transcript,
        )

    def test_excerpt_covers_beginning_middle_and_end(self) -> None:
        text = "BEGIN-" + ("A" * 150) + "-MIDDLE-" + ("B" * 150) + "-END"
        excerpt, truncated = transcript_excerpt(text, 180)
        self.assertTrue(truncated)
        self.assertLessEqual(len(excerpt), 180)
        self.assertTrue(excerpt.startswith("BEGIN-"))
        self.assertIn("transcript middle excerpt", excerpt)
        self.assertIn("transcript final excerpt", excerpt)
        self.assertTrue(excerpt.endswith("-END"))

    def test_refinement_targets_parent_and_reports_missing_transcript(self) -> None:
        with open_catalogue(self.db_path) as conn:
            target = transcript_refinement_evidence(
                conn,
                self.parent_run,
                max_transcript_chars=160,
            )
        self.assertEqual(target.snapshot_id, self.snapshot)
        self.assertEqual([item.video_id for item in target.videos], ["video00000A"])
        self.assertEqual(target.missing_transcript_video_ids, ("video00000E",))
        item = target.videos[0]
        self.assertEqual(item.parent_run_id, self.parent_run)
        self.assertEqual(item.transcript_source_type, "manual")
        self.assertEqual(item.transcript_language, "en")
        self.assertEqual(item.transcript_format, "json3")
        self.assertTrue(item.transcript_truncated)
        self.assertEqual(item.transcript_request_key, "request-key")
        self.assertTrue(item.previous_classification["needs_transcript"])

    def test_current_human_decision_excludes_transcript_refinement(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Mystery A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            target = transcript_refinement_evidence(conn, self.parent_run)
        self.assertEqual(target.videos, ())
        self.assertEqual(target.missing_transcript_video_ids, ("video00000E",))

    def test_transcript_prompt_has_distinct_hash_and_guidance(self) -> None:
        refined = transcript_refinement_prompt(self.prompt)
        self.assertNotEqual(refined.sha256, self.prompt.sha256)
        self.assertIn("transcript/caption refinement", refined.system)
        self.assertIn("automatic captions", refined.system)

    def test_child_run_records_transcript_context_without_decision(self) -> None:
        refined_prompt = transcript_refinement_prompt(self.prompt)
        with open_catalogue(self.db_path) as conn:
            target = transcript_refinement_evidence(conn, self.parent_run)
            videos = list(target.videos)
            suggestion = self._suggestion("video00000A", False)
            batch = ClassificationBatchResult(
                suggestions=(suggestion,),
                input_sha256="child-batch",
                usage={"prompt_tokens": 12},
                response_model="model-a",
            )
            child = store_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=refined_prompt,
                videos=videos,
                result=ClassificationRunResult(
                    suggestions=(suggestion,),
                    batches=(batch,),
                ),
                context={
                    "stage": "transcript_refinement",
                    "parent_run_id": self.parent_run,
                    "max_transcript_chars": 12000,
                },
            )
            payload = run_payload(conn, child)
            decision_count = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]

        self.assertEqual(payload["context"]["stage"], "transcript_refinement")
        self.assertEqual(payload["context"]["parent_run_id"], self.parent_run)
        self.assertFalse(payload["classifications"][0]["needs_transcript"])
        self.assertEqual(decision_count, 0)


if __name__ == "__main__":
    unittest.main()
