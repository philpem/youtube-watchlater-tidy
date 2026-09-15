from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.enrichment import store_observation
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_classification import (
    ClassificationEvidence,
    classification_evidence,
    classify,
    validate_response,
)
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.llm_prompt import RenderedPrompt
from youtube_watchlater_tidy.llm_provider import ChatResponse
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class LLMClassificationTests(unittest.TestCase):
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
                            "title": "YOU WON'T BELIEVE THIS",
                            "channel_id": "UCONE",
                            "channel": "One",
                            "duration": 0,
                            "view_count": 0,
                        },
                        {"id": "video00000E", "title": "[Deleted video]"},
                        {"id": "video00000I", "title": "Already decided", "channel": "Three"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            store_observation(
                conn,
                "video00000E",
                "wayback-via-findyoutubevideo",
                "found",
                {
                    "id": "video00000E",
                    "title": "Recovered technical talk",
                    "channel_id": "UCTWO",
                    "channel": "Two",
                    "duration": 321,
                    "view_count": 456,
                    "upload_date": "2019-01-02",
                },
            )
            selection = select_title(conn, contains="Already decided", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)

            conn.execute(
                """
                INSERT INTO dearrow_lookups (
                    video_id, looked_up_at, status, preferred_title,
                    titles_json, source_url, raw_json
                ) VALUES (?, ?, 'found', ?, '[]', NULL, '{}')
                """,
                (
                    "video00000A",
                    "2026-09-15T00:00:00+00:00",
                    "A descriptive title",
                ),
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_evidence_uses_recovery_dearrow_and_only_unresolved(self) -> None:
        with open_catalogue(self.db_path) as conn:
            rows = classification_evidence(conn, self.snapshot)
        self.assertEqual([row.video_id for row in rows], ["video00000A", "video00000E"])
        first, second = rows
        self.assertEqual(first.original_title, "YOU WON'T BELIEVE THIS")
        self.assertEqual(first.dearrow_title, "A descriptive title")
        # Zero is valid source data and must not be replaced by fallback metadata.
        self.assertEqual(first.duration, 0)
        self.assertEqual(first.view_count, 0)
        self.assertEqual(second.original_title, "[Deleted video]")
        self.assertEqual(second.recovered_title, "Recovered technical talk")
        self.assertEqual(second.recovered_source, "wayback-via-findyoutubevideo")
        self.assertEqual(second.channel_id, "UCTWO")

    def test_selection_target_still_excludes_decided_videos(self) -> None:
        with open_catalogue(self.db_path) as conn:
            selection = select_title(conn, regex=r".*", snapshot_id=self.snapshot)
            rows = classification_evidence(
                conn,
                selection_id=selection.selection_id,
            )
        self.assertEqual([row.video_id for row in rows], ["video00000A", "video00000E"])

    @staticmethod
    def _valid(video_id: str, *, action: str = "move") -> dict:
        return {
            "video_id": video_id,
            "action": action,
            "topic": "retrocomputing",
            "content_type": "technical talk",
            "timeliness": "evergreen",
            "quality": 0.8,
            "confidence": 0.9,
            "reason": "Strong match for the user's technical interests.",
            "destination": {
                "existing_playlist": "Queue - Retrocomputing" if action == "move" else None,
                "new_queue_proposal": None,
                "confidence": 0.9,
                "reason": "Matches the existing queue.",
            },
            "needs_description": False,
            "needs_transcript": False,
        }

    def test_validation_accepts_controlled_existing_playlist(self) -> None:
        suggestions = validate_response(
            {"classifications": [self._valid("video00000A")]},
            expected_video_ids=["video00000A"],
            existing_playlists={"Queue - Retrocomputing"},
        )
        self.assertEqual(suggestions[0].existing_playlist, "Queue - Retrocomputing")
        self.assertEqual(suggestions[0].action, "move")

    def test_validation_rejects_unknown_existing_playlist(self) -> None:
        row = self._valid("video00000A")
        row["destination"]["existing_playlist"] = "Made Up Playlist"
        with self.assertRaisesRegex(ValueError, "unknown existing playlist"):
            validate_response(
                {"classifications": [row]},
                expected_video_ids=["video00000A"],
                existing_playlists={"Queue - Retrocomputing"},
            )

    def test_validation_rejects_move_without_destination_and_missing_ids(self) -> None:
        row = self._valid("video00000A")
        row["destination"]["existing_playlist"] = None
        with self.assertRaisesRegex(ValueError, "no destination"):
            validate_response(
                {"classifications": [row]},
                expected_video_ids=["video00000A"],
                existing_playlists=set(),
            )

        with self.assertRaisesRegex(ValueError, "omitted"):
            validate_response(
                {"classifications": [self._valid("video00000A", action="review")]},
                expected_video_ids=["video00000A", "video00000E"],
                existing_playlists={"Queue - Retrocomputing"},
            )

    def test_classify_batches_in_parallel_but_preserves_input_order(self) -> None:
        videos = [
            ClassificationEvidence(
                video_id="video00000A",
                playlist_position=1,
                original_title="A",
                recovered_title=None,
                recovered_source=None,
                dearrow_title=None,
                channel="One",
                channel_id="UCONE",
                duration=1,
                view_count=2,
                upload_date=None,
                availability=None,
            ),
            ClassificationEvidence(
                video_id="video00000E",
                playlist_position=2,
                original_title="B",
                recovered_title=None,
                recovered_source=None,
                dearrow_title=None,
                channel="Two",
                channel_id="UCTWO",
                duration=3,
                view_count=4,
                upload_date=None,
                availability=None,
            ),
        ]
        provider = ProviderConfig(
            name="test",
            preset="generic",
            base_url="https://example.invalid/v1",
            model="model-a",
            concurrency=2,
        )
        prompt = RenderedPrompt(
            system="system",
            interest_brief="interests",
            playlist_guidance="- Queue - Retrocomputing: retro",
            profile_name="default",
            sha256="prompt-hash",
        )

        def fake_chat(provider, messages, json_schema=None):
            batch = json.loads(messages[-1]["content"].split("\n", 1)[1])["videos"]
            video_id = batch[0]["video_id"]
            payload = {"classifications": [self._valid(video_id)]}
            return ChatResponse(
                content=json.dumps(payload),
                usage={"prompt_tokens": 10},
                model="model-a",
                raw={},
            )

        with patch("youtube_watchlater_tidy.llm_classification.chat", side_effect=fake_chat):
            result = classify(
                provider,
                prompt,
                videos,
                playlists={"Queue - Retrocomputing"},
                batch_size=1,
            )
        self.assertEqual(
            [item.video_id for item in result.suggestions],
            ["video00000A", "video00000E"],
        )
        self.assertEqual(len(result.batches), 2)
        self.assertTrue(all(batch.input_sha256 for batch in result.batches))


if __name__ == "__main__":
    unittest.main()
