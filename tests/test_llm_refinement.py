from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.enrichment import store_observation
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_classification import (
    ClassificationBatchResult,
    ClassificationEvidence,
    ClassificationRunResult,
    ClassificationSuggestion,
)
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.llm_prompt import RenderedPrompt
from youtube_watchlater_tidy.llm_refinement import (
    description_refinement_evidence,
    description_refinement_prompt,
)
from youtube_watchlater_tidy.llm_store import run_payload, store_run
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class LLMRefinementTests(unittest.TestCase):
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
            suggestions = tuple(
                self._suggestion(video.video_id, needs_description=True) for video in videos
            )
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
            store_observation(
                conn,
                "video00000A",
                "yt-dlp",
                "found",
                {
                    "id": "video00000A",
                    "title": "Mystery A",
                    "description": "A long technical description about reverse engineering.",
                },
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _suggestion(video_id: str, *, needs_description: bool) -> ClassificationSuggestion:
        return ClassificationSuggestion(
            video_id=video_id,
            action="review",
            topic="unknown",
            content_type="unknown",
            timeliness="unknown",
            quality=0.5,
            confidence=0.4,
            reason="Not enough evidence yet.",
            existing_playlist=None,
            new_queue_proposal=None,
            destination_confidence=0.0,
            destination_reason="",
            needs_description=needs_description,
            needs_transcript=False,
        )

    def test_refinement_targets_parent_needs_description_and_reports_missing(self) -> None:
        with open_catalogue(self.db_path) as conn:
            target = description_refinement_evidence(
                conn,
                self.parent_run,
                max_description_chars=20,
            )

        self.assertEqual(target.snapshot_id, self.snapshot)
        self.assertEqual([item.video_id for item in target.videos], ["video00000A"])
        self.assertEqual(target.missing_description_video_ids, ("video00000E",))
        item = target.videos[0]
        self.assertEqual(item.parent_run_id, self.parent_run)
        self.assertEqual(item.description_source, "yt-dlp")
        self.assertEqual(item.description, "A long technical des")
        self.assertTrue(item.description_truncated)
        self.assertEqual(item.previous_evidence["video_id"], "video00000A")
        self.assertTrue(item.previous_classification["needs_description"])

    def test_current_human_decision_still_excludes_refinement(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="Mystery A", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            target = description_refinement_evidence(conn, self.parent_run)

        self.assertEqual(target.videos, ())
        self.assertEqual(target.missing_description_video_ids, ("video00000E",))

    def test_refinement_prompt_has_distinct_hash_and_guidance(self) -> None:
        refined = description_refinement_prompt(self.prompt)
        self.assertNotEqual(refined.sha256, self.prompt.sha256)
        self.assertIn("description refinement", refined.system)
        self.assertIn("needs_transcript", refined.system)

    def test_child_run_records_parent_context_without_creating_decision(self) -> None:
        refined_prompt = description_refinement_prompt(self.prompt)
        with open_catalogue(self.db_path) as conn:
            target = description_refinement_evidence(conn, self.parent_run)
            videos = list(target.videos)
            suggestion = self._suggestion("video00000A", needs_description=False)
            batch = ClassificationBatchResult(
                suggestions=(suggestion,),
                input_sha256="child-batch",
                usage={"prompt_tokens": 10},
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
                    "stage": "description_refinement",
                    "parent_run_id": self.parent_run,
                    "max_description_chars": 4000,
                },
            )
            payload = run_payload(conn, child)
            decision_count = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]

        self.assertEqual(payload["context"]["stage"], "description_refinement")
        self.assertEqual(payload["context"]["parent_run_id"], self.parent_run)
        self.assertEqual(payload["classifications"][0]["needs_description"], False)
        self.assertEqual(decision_count, 0)


if __name__ == "__main__":
    unittest.main()
