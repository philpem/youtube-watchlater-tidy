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
    ClassificationRunResult,
    ClassificationSuggestion,
    classification_evidence,
)
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.llm_prompt import RenderedPrompt
from youtube_watchlater_tidy.llm_store import store_run
from youtube_watchlater_tidy.rich_metadata import candidate_video_ids, enrich_rich_metadata
from youtube_watchlater_tidy.triage import select_title


class RichMetadataTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "Needs context", "channel": "One"},
                        {
                            "id": "video00000E",
                            "title": "Already described",
                            "channel": "Two",
                            "description": "Description from the source snapshot",
                        },
                        {"id": "video00000I", "title": "[Deleted video]"},
                        {"id": "video00000M", "title": "Previously fetched", "channel": "Four"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_description_skips_existing_unavailable_and_cached(self) -> None:
        with open_catalogue(self.db_path) as conn:
            store_observation(
                conn,
                "video00000M",
                "yt-dlp",
                "found",
                {"id": "video00000M", "title": "Previously fetched", "description": None},
            )
            self.assertEqual(
                candidate_video_ids(
                    conn,
                    self.snapshot,
                    missing_description=True,
                ),
                ["video00000A"],
            )
            self.assertEqual(
                candidate_video_ids(
                    conn,
                    self.snapshot,
                    missing_description=True,
                    refresh=True,
                ),
                ["video00000A", "video00000M"],
            )

    def test_selection_and_explicit_targets(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, contains="context", snapshot_id=self.snapshot)
            self.assertEqual(
                candidate_video_ids(conn, selection_id=selection.selection_id),
                ["video00000A"],
            )
            self.assertEqual(
                candidate_video_ids(
                    conn,
                    self.snapshot,
                    video_ids=["video00000M", "video00000A"],
                ),
                ["video00000A", "video00000M"],
            )

    @staticmethod
    def _suggestion(video_id: str, needs_description: bool) -> ClassificationSuggestion:
        return ClassificationSuggestion(
            video_id=video_id,
            action="review",
            topic="unknown",
            content_type="unknown",
            timeliness="unknown",
            quality=0.5,
            confidence=0.4,
            reason="Need more context.",
            existing_playlist=None,
            new_queue_proposal=None,
            destination_confidence=0.0,
            destination_reason="",
            needs_description=needs_description,
            needs_transcript=False,
        )

    def test_llm_needs_description_targets_only_flagged_videos(self) -> None:
        provider = ProviderConfig(
            name="local",
            preset="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3:14b",
        )
        prompt = RenderedPrompt(
            system="system",
            interest_brief="interest",
            playlist_guidance="",
            profile_name="default",
            sha256="prompt-sha",
        )
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(conn, self.snapshot)
            suggestions = tuple(
                self._suggestion(video.video_id, video.video_id == "video00000A")
                for video in videos
            )
            result = ClassificationRunResult(
                suggestions=suggestions,
                batches=(
                    ClassificationBatchResult(
                        suggestions=suggestions,
                        input_sha256="batch-sha",
                        usage={},
                        response_model="qwen3:14b",
                    ),
                ),
            )
            run_id = store_run(
                conn,
                snapshot_id=self.snapshot,
                provider=provider,
                prompt=prompt,
                videos=videos,
                result=result,
            )
            candidates = candidate_video_ids(
                conn,
                llm_needs_description=True,
                run_id=run_id,
            )
        self.assertEqual(candidates, ["video00000A"])

    def test_enrichment_stores_description_and_fails_per_video(self) -> None:
        def fetcher(video_id: str):
            if video_id == "video00000M":
                raise RuntimeError("temporary failure")
            return {
                "id": video_id,
                "title": "Fetched title",
                "description": "A long useful description",
                "upload_date": "20200102",
                "live_status": "not_live",
                "availability": "public",
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            }

        with open_catalogue(self.db_path) as conn:
            result = enrich_rich_metadata(
                conn,
                ["video00000A", "video00000M"],
                fetcher=fetcher,
                workers=2,
                start_interval=0,
                show_progress=False,
            )
            found = conn.execute(
                """
                SELECT status, description, upload_date, raw_json
                FROM metadata_observations
                WHERE video_id = 'video00000A' AND source = 'yt-dlp'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            failed = conn.execute(
                """
                SELECT status, raw_json FROM metadata_observations
                WHERE video_id = 'video00000M' AND source = 'yt-dlp'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()

        self.assertEqual((result.found, result.failed), (1, 1))
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["description"], "A long useful description")
        self.assertEqual(found["upload_date"], "20200102")
        self.assertEqual(json.loads(found["raw_json"])["live_status"], "not_live")
        self.assertEqual(failed["status"], "error")
        self.assertIn("temporary failure", json.loads(failed["raw_json"])["error"])


if __name__ == "__main__":
    unittest.main()
