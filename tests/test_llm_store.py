from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import SCHEMA_VERSION, ensure_schema, open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_classification import (
    ClassificationBatchResult,
    ClassificationRunResult,
    ClassificationSuggestion,
    classification_evidence,
)
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.llm_prompt import RenderedPrompt
from youtube_watchlater_tidy.llm_store import (
    cached_run_id,
    classification_cache_key,
    provider_fingerprint,
    run_payload,
    store_run,
)
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class LLMStoreTests(unittest.TestCase):
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
                        {
                            "id": "video00000A",
                            "title": "Reverse engineering a Z80 board",
                            "channel_id": "UCRETRO",
                            "channel": "Retro Lab",
                            "duration": 600,
                            "view_count": 1234,
                        },
                        {
                            "id": "video00000E",
                            "title": "Other video",
                            "channel_id": "UCOTHER",
                            "channel": "Other",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id

        self.provider = ProviderConfig(
            name="local",
            preset="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3:14b",
            temperature=0.0,
            max_tokens=1200,
        )
        self.prompt = RenderedPrompt(
            system="system",
            interest_brief="retrocomputing",
            playlist_guidance="- Queue - Retrocomputing: retro",
            profile_name="default",
            sha256="prompt-sha",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _suggestion(video_id: str) -> ClassificationSuggestion:
        return ClassificationSuggestion(
            video_id=video_id,
            action="move",
            topic="retrocomputing",
            content_type="technical",
            timeliness="evergreen",
            quality=0.8,
            confidence=0.9,
            reason="Strong technical match.",
            existing_playlist="Queue - Retrocomputing",
            new_queue_proposal=None,
            destination_confidence=0.9,
            destination_reason="Fits the queue.",
            needs_description=False,
            needs_transcript=False,
        )

    def test_schema_v6_migrates_to_current_version(self) -> None:
        path = Path(self.tmp.name) / "v6.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA user_version = 6")
        ensure_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("llm_classification_runs", tables)
        self.assertIn("llm_classification_batches", tables)
        self.assertIn("llm_classifications", tables)

    def test_provider_fingerprint_changes_with_generation_settings_not_secret_value(self) -> None:
        first = provider_fingerprint(self.provider)
        changed = provider_fingerprint(
            ProviderConfig(
                name="local",
                preset="ollama",
                base_url="http://127.0.0.1:11434/v1",
                model="qwen3:14b",
                temperature=0.2,
            )
        )
        self.assertNotEqual(first, changed)

        with_key_name = provider_fingerprint(
            ProviderConfig(
                name="local",
                preset="ollama",
                base_url="http://127.0.0.1:11434/v1",
                model="qwen3:14b",
                api_key_env="SOME_KEY",
                temperature=0.0,
                max_tokens=1200,
            )
        )
        self.assertEqual(first, with_key_name)

    def test_store_run_is_cached_and_never_creates_a_decision(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(conn, self.snapshot, limit=1)
            suggestion = self._suggestion(videos[0].video_id)
            batch = ClassificationBatchResult(
                suggestions=(suggestion,),
                input_sha256="batch-sha",
                usage={"prompt_tokens": 100, "completion_tokens": 20},
                response_model="qwen3:14b",
            )
            result = ClassificationRunResult(
                suggestions=(suggestion,),
                batches=(batch,),
            )
            provider_sha, input_sha, _ = classification_cache_key(
                self.provider, self.prompt, videos
            )
            run_id = store_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                result=result,
            )
            cached = cached_run_id(
                conn,
                snapshot_id=self.snapshot,
                provider_sha256=provider_sha,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
            )
            decision_count = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            payload = run_payload(conn, run_id)

        self.assertEqual(cached, run_id)
        self.assertEqual(decision_count, 0)
        self.assertEqual(payload["classifications"][0]["action"], "move")
        self.assertIsNone(payload["classifications"][0]["current_decision"])
        self.assertEqual(payload["batches"][0]["usage"]["prompt_tokens"], 100)

    def test_later_human_decision_is_displayed_without_mutating_stored_suggestion(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(conn, self.snapshot, limit=1)
            suggestion = self._suggestion(videos[0].video_id)
            result = ClassificationRunResult(
                suggestions=(suggestion,),
                batches=(
                    ClassificationBatchResult(
                        suggestions=(suggestion,),
                        input_sha256="batch-sha",
                        usage={},
                        response_model="qwen3:14b",
                    ),
                ),
            )
            run_id = store_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                result=result,
            )
            selection = select_title(
                conn,
                contains="Reverse engineering",
                snapshot_id=self.snapshot,
            )
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            payload = run_payload(conn, run_id)

        classification = payload["classifications"][0]
        self.assertEqual(classification["action"], "move")
        self.assertEqual(classification["current_decision"]["action"], "keep")
        self.assertEqual(classification["current_decision"]["source"], "human")


if __name__ == "__main__":
    unittest.main()
