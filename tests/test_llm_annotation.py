from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.db import SCHEMA_VERSION, ensure_schema, open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_annotation import (
    AnnotationBatchResult,
    AnnotationPrompt,
    AnnotationRunResult,
    SemanticAnnotation,
    annotate,
    discover_taxonomy,
    render_annotation_prompt,
    validate_annotation_response,
)
from youtube_watchlater_tidy.llm_annotation_store import (
    annotation_cache_key,
    annotation_run_payload,
    cached_annotation_run_id,
    store_annotation_run,
)
from youtube_watchlater_tidy.llm_classification import (
    ClassificationEvidence,
    classification_evidence,
)
from youtube_watchlater_tidy.llm_config import ProviderConfig, load_project_config
from youtube_watchlater_tidy.llm_provider import ChatResponse
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class LLMAnnotationTests(unittest.TestCase):
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
                            "title": "Repairing an Acorn Archimedes",
                            "channel_id": "UCRETRO",
                            "channel": "Retro Lab",
                            "duration": 600,
                            "view_count": 100,
                        },
                        {
                            "id": "video00000E",
                            "title": "Understanding V.34 modems",
                            "channel_id": "UCTEL",
                            "channel": "Telecom Lab",
                            "duration": 900,
                            "view_count": 200,
                        },
                        {
                            "id": "video00000I",
                            "title": "Already decided electronics video",
                            "channel_id": "UCELEC",
                            "channel": "Electronics Lab",
                            "duration": 300,
                            "view_count": 300,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            selection = select_title(
                conn,
                contains="Already decided",
                snapshot_id=self.snapshot,
            )
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)

        self.provider = ProviderConfig(
            name="test",
            preset="generic",
            base_url="https://example.invalid/v1",
            model="model-a",
            concurrency=2,
        )
        self.categories = {
            "Retrocomputing": "Historic computers and operating systems.",
            "Telecoms": "Telephony, radio, networking and modems.",
            "Electronics": "Electronics and hardware engineering.",
            "Other": "Material outside the main vocabulary.",
            "Unclear": "Insufficient evidence.",
        }
        self.prompt = AnnotationPrompt(
            system="system",
            interest_brief="retrocomputing and telecoms",
            profile_name="default",
            categories=self.categories,
            sha256="annotation-prompt",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _annotation(video_id: str, category: str = "Retrocomputing") -> SemanticAnnotation:
        return SemanticAnnotation(
            video_id=video_id,
            primary_category=category,
            subject="Acorn Archimedes hardware repair",
            tags=("acorn", "archimedes", "hardware repair"),
            content_type="repair",
            confidence=0.9,
        )

    def test_evidence_can_include_already_decided_videos(self) -> None:
        with open_catalogue(self.db_path) as conn:
            remaining = classification_evidence(conn, self.snapshot)
            all_rows = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
            )
        self.assertEqual(
            [row.video_id for row in remaining],
            ["video00000A", "video00000E"],
        )
        self.assertEqual(
            [row.video_id for row in all_rows],
            ["video00000A", "video00000E", "video00000I"],
        )

    def test_configured_categories_are_in_prompt_with_reserved_fallbacks(self) -> None:
        config_path = self.root / "watchlater.toml"
        config_path.write_text(
            """
default_provider = "local"

[providers.local]
preset = "ollama"
model = "qwen3:14b"

[llm.review_categories]
Retrocomputing = "Historic computers and unusual architectures."
Telecoms = "Telephony, radio, networking and modems."
""".lstrip(),
            encoding="utf-8",
        )
        config = load_project_config(config_path)
        prompt = render_annotation_prompt(config, config.review_categories)
        self.assertEqual(
            config.review_categories["Retrocomputing"],
            "Historic computers and unusual architectures.",
        )
        self.assertIn("Retrocomputing", prompt.categories)
        self.assertIn("Telecoms", prompt.categories)
        self.assertIn("Other", prompt.categories)
        self.assertIn("Unclear", prompt.categories)
        self.assertIn("primary_category", json.dumps(prompt.schema()))

    def test_validation_normalises_tags_and_enforces_category(self) -> None:
        value = {
            "annotations": [
                {
                    "video_id": "video00000A",
                    "primary_category": "Retrocomputing",
                    "subject": "Acorn Archimedes repair",
                    "tags": ["#Acorn", "  ARCHIMEDES  ", "Acorn"],
                    "content_type": "repair",
                    "confidence": 0.92,
                }
            ]
        }
        rows = validate_annotation_response(
            value,
            expected_video_ids=["video00000A"],
            categories=set(self.categories),
        )
        self.assertEqual(rows[0].tags, ("acorn", "archimedes"))

        value["annotations"][0]["primary_category"] = "Made up"
        with self.assertRaisesRegex(ValueError, "controlled vocabulary"):
            validate_annotation_response(
                value,
                expected_video_ids=["video00000A"],
                categories=set(self.categories),
            )

    def test_annotation_batches_preserve_input_order(self) -> None:
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

        def fake_chat(provider, messages, json_schema=None, **kwargs):
            batch = json.loads(messages[-1]["content"].split("\n", 1)[1])["videos"]
            video_id = batch[0]["video_id"]
            payload = {
                "annotations": [
                    {
                        "video_id": video_id,
                        "primary_category": "Retrocomputing",
                        "subject": "subject " + video_id,
                        "tags": ["retrocomputing", "hardware"],
                        "content_type": "technical",
                        "confidence": 0.8,
                    }
                ]
            }
            return ChatResponse(
                content=json.dumps(payload),
                usage={"prompt_tokens": 10},
                model="model-a",
                raw={},
            )

        events = []
        with patch("youtube_watchlater_tidy.llm_annotation.chat", side_effect=fake_chat):
            result = annotate(
                self.provider,
                self.prompt,
                videos,
                batch_size=1,
                progress=events.append,
            )

        self.assertEqual(
            [item.video_id for item in result.annotations],
            ["video00000A", "video00000E"],
        )
        self.assertEqual(len(result.batches), 2)
        self.assertEqual(events[0].kind, "start")
        self.assertEqual(events[-1].kind, "finish")
        self.assertEqual(
            [event.completed for event in events if event.kind == "update"],
            [1, 2],
        )

    def test_taxonomy_discovery_adds_reserved_categories(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
            )

        def fake_chat(provider, messages, json_schema=None, **kwargs):
            return ChatResponse(
                content=json.dumps(
                    {
                        "categories": [
                            {"name": "Retrocomputing", "description": "Historic computers."},
                            {"name": "Telecoms", "description": "Communications systems."},
                            {"name": "Electronics", "description": "Hardware engineering."},
                        ]
                    }
                ),
                usage={"prompt_tokens": 20},
                model="model-a",
                raw={},
            )

        with patch("youtube_watchlater_tidy.llm_annotation.chat", side_effect=fake_chat):
            result = discover_taxonomy(
                self.provider,
                videos,
                interest_brief="technical material",
                max_categories=10,
            )
        self.assertIn("Retrocomputing", result.categories)
        self.assertIn("Other", result.categories)
        self.assertIn("Unclear", result.categories)
        self.assertTrue(result.input_sha256)
        self.assertTrue(result.prompt_sha256)

    def test_annotation_store_is_cached_and_does_not_create_decisions(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
                limit=1,
            )
            annotation = self._annotation(videos[0].video_id)
            result = AnnotationRunResult(
                annotations=(annotation,),
                batches=(
                    AnnotationBatchResult(
                        annotations=(annotation,),
                        input_sha256="batch-sha",
                        usage={"prompt_tokens": 10},
                        response_model="model-a",
                    ),
                ),
            )
            provider_sha, input_sha, _ = annotation_cache_key(
                self.provider,
                self.prompt,
                videos,
            )
            before = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            run_id = store_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                result=result,
                taxonomy_source="configured",
                context={"scope": "all"},
            )
            after = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
            cached = cached_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                provider_sha256=provider_sha,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
            )
            payload = annotation_run_payload(conn, run_id)

        self.assertEqual(before, after)
        self.assertEqual(cached, run_id)
        self.assertEqual(payload["taxonomy_source"], "configured")
        self.assertEqual(payload["annotations"][0]["primary_category"], "Retrocomputing")
        self.assertEqual(payload["context"]["scope"], "all")

    def test_schema_v7_migrates_annotation_tables(self) -> None:
        path = self.root / "v7.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA user_version = 7")
        ensure_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("llm_annotation_runs", tables)
        self.assertIn("llm_annotation_batches", tables)
        self.assertIn("llm_annotations", tables)


if __name__ == "__main__":
    unittest.main()
