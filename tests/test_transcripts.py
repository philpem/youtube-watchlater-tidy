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
from youtube_watchlater_tidy.transcripts import (
    candidate_video_ids,
    fetch_transcripts,
    latest_transcript,
    parse_caption_payload,
    select_caption_track,
)


class TranscriptTests(unittest.TestCase):
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
                        {"id": "video00000A", "title": "A", "channel": "One"},
                        {"id": "video00000E", "title": "B", "channel": "Two"},
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
    def _json3(text: str) -> str:
        return json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 100,
                        "dDurationMs": 2000,
                        "segs": [{"utf8": text}],
                    }
                ]
            }
        )

    def test_manual_track_wins_and_auto_is_fallback(self) -> None:
        metadata = {
            "subtitles": {
                "en-uYU": [
                    {
                        "ext": "json3",
                        "name": "English - CC1",
                        "data": self._json3("manual words"),
                    }
                ]
            },
            "automatic_captions": {
                "en-orig": [
                    {
                        "ext": "json3",
                        "name": "English (Original)",
                        "data": self._json3("automatic words"),
                    }
                ]
            },
        }
        manual = select_caption_track(metadata, languages=["en"], allow_automatic=True)
        self.assertEqual(manual.source_type, "manual")
        self.assertEqual(manual.language, "en-uYU")

        metadata["subtitles"] = {}
        automatic = select_caption_track(metadata, languages=["en"], allow_automatic=True)
        self.assertEqual(automatic.source_type, "automatic")
        self.assertEqual(automatic.language, "en-orig")
        self.assertIsNone(
            select_caption_track(metadata, languages=["en"], allow_automatic=False)
        )

    def test_json3_and_vtt_are_normalized(self) -> None:
        parsed = parse_caption_payload(
            "json3",
            json.dumps(
                {
                    "events": [
                        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
                        {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "Next line"}]},
                    ]
                }
            ),
        )
        self.assertEqual(parsed.text, "Hello world\nNext line")
        self.assertEqual(parsed.segments[0]["start_ms"], 0)

        vtt = parse_caption_payload(
            "vtt",
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello &amp; goodbye\n\n00:00:01.000 --> 00:00:02.000\n<b>Next</b> line\n",
        )
        self.assertEqual(vtt.text, "Hello & goodbye\nNext line")

    def test_fetch_stores_transcript_and_cache_skips_same_policy(self) -> None:
        def metadata_fetcher(video_id: str):
            self.assertEqual(video_id, "video00000A")
            return {
                "id": video_id,
                "subtitles": {
                    "en": [
                        {
                            "ext": "json3",
                            "name": "English",
                            "data": self._json3("A useful transcript"),
                        }
                    ]
                },
                "automatic_captions": {},
            }

        with open_catalogue(self.db_path) as conn:
            result = fetch_transcripts(
                conn,
                ["video00000A"],
                languages=["en"],
                metadata_fetcher=metadata_fetcher,
                workers=1,
                start_interval=0,
                show_progress=False,
            )
            row = latest_transcript(conn, "video00000A")
            candidates = candidate_video_ids(
                conn,
                self.snapshot,
                video_ids=["video00000A"],
                languages=["en"],
                allow_automatic=True,
            )
            refreshed = candidate_video_ids(
                conn,
                self.snapshot,
                video_ids=["video00000A"],
                languages=["en"],
                allow_automatic=True,
                refresh=True,
            )

        self.assertEqual((result.found, result.manual, result.automatic), (1, 1, 0))
        self.assertEqual(row["transcript_text"], "A useful transcript")
        self.assertEqual(row["source_type"], "manual")
        self.assertEqual(candidates, [])
        self.assertEqual(refreshed, ["video00000A"])

    def test_no_caption_result_is_cached_for_policy(self) -> None:
        with open_catalogue(self.db_path) as conn:
            result = fetch_transcripts(
                conn,
                ["video00000A"],
                languages=["en"],
                metadata_fetcher=lambda video_id: {
                    "id": video_id,
                    "subtitles": {},
                    "automatic_captions": {},
                },
                workers=1,
                start_interval=0,
                show_progress=False,
            )
            same_policy = candidate_video_ids(
                conn,
                self.snapshot,
                video_ids=["video00000A"],
                languages=["en"],
                allow_automatic=True,
            )
            different_policy = candidate_video_ids(
                conn,
                self.snapshot,
                video_ids=["video00000A"],
                languages=["fr"],
                allow_automatic=True,
            )
        self.assertEqual(result.not_found, 1)
        self.assertEqual(same_policy, [])
        self.assertEqual(different_policy, ["video00000A"])

    def test_llm_needs_transcript_target(self) -> None:
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
            sha256="prompt",
        )
        video = ClassificationEvidence(
            video_id="video00000E",
            playlist_position=2,
            original_title="B",
            recovered_title=None,
            recovered_source=None,
            dearrow_title=None,
            channel="Two",
            channel_id=None,
            duration=None,
            view_count=None,
            upload_date=None,
            availability=None,
        )
        suggestion = ClassificationSuggestion(
            video_id="video00000E",
            action="review",
            topic="unknown",
            content_type="unknown",
            timeliness="unknown",
            quality=0.5,
            confidence=0.4,
            reason="Need spoken context",
            existing_playlist=None,
            new_queue_proposal=None,
            destination_confidence=0.0,
            destination_reason="",
            needs_description=False,
            needs_transcript=True,
        )
        batch = ClassificationBatchResult(
            suggestions=(suggestion,),
            input_sha256="batch",
            usage={},
            response_model="model-a",
        )
        with open_catalogue(self.db_path) as conn:
            run_id = store_run(
                conn,
                snapshot_id=self.snapshot,
                provider=provider,
                prompt=prompt,
                videos=[video],
                result=ClassificationRunResult(
                    suggestions=(suggestion,), batches=(batch,)
                ),
            )
            candidates = candidate_video_ids(
                conn,
                llm_needs_transcript=True,
                run_id=run_id,
                languages=["en"],
            )
        self.assertEqual(candidates, ["video00000E"])


if __name__ == "__main__":
    unittest.main()
