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
    TaxonomyDiscovery,
    annotate,
    discover_taxonomy,
    render_annotation_prompt,
    validate_annotation_response,
)
from youtube_watchlater_tidy.llm_annotation_store import (
    annotation_cache_key,
    annotation_completed_batch_indexes,
    annotation_run_payload,
    begin_annotation_run,
    cached_annotation_run_id,
    complete_annotation_run,
    incomplete_annotation_run_id,
    reusable_annotation_run_id,
    store_annotation_batch,
    store_annotation_run,
)
from youtube_watchlater_tidy.llm_classification import (
    ClassificationEvidence,
    classification_evidence,
    evidence_hash,
)
from youtube_watchlater_tidy.llm_config import ProviderConfig, load_project_config
from youtube_watchlater_tidy.llm_provider import ChatResponse
from youtube_watchlater_tidy.llm_taxonomy_store import (
    store_taxonomy,
    taxonomy_categories,
    taxonomy_list_payload,
    taxonomy_payload,
)
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
        self.assertIn("concurrency=2", events[0].detail or "")
        status = next(event for event in events if event.kind == "status")
        self.assertIn("2 request(s) in flight", status.detail or "")
        self.assertIn("0 queued", status.detail or "")
        self.assertEqual(events[-1].kind, "finish")
        self.assertEqual(
            [event.completed for event in events if event.kind == "update"],
            [1, 2],
        )

    def test_truncated_annotation_batch_recursively_splits_and_preserves_order(self) -> None:
        videos = [
            ClassificationEvidence(
                video_id=f"video00000{suffix}",
                playlist_position=index,
                original_title=f"Video {suffix}",
                recovered_title=None,
                recovered_source=None,
                dearrow_title=None,
                channel="Test",
                channel_id="UCTEST",
                duration=1,
                view_count=1,
                upload_date=None,
                availability=None,
            )
            for index, suffix in enumerate(("A", "B", "C", "D"), start=1)
        ]
        calls: list[int] = []

        def fake_chat(provider, messages, json_schema=None, **kwargs):
            batch = json.loads(messages[-1]["content"].split("\n", 1)[1])["videos"]
            calls.append(len(batch))
            usage = {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 1},
            }
            if len(batch) > 1:
                return ChatResponse(
                    content="{",
                    usage=usage,
                    model="model-a",
                    raw={},
                    finish_reason="length",
                )
            video_id = batch[0]["video_id"]
            return ChatResponse(
                content=json.dumps(
                    {
                        "annotations": [
                            {
                                "video_id": video_id,
                                "primary_category": "Retrocomputing",
                                "subject": "subject " + video_id,
                                "tags": ["retrocomputing"],
                                "content_type": "technical",
                                "confidence": 0.8,
                            }
                        ]
                    }
                ),
                usage=usage,
                model="model-a",
                raw={},
                finish_reason="stop",
            )

        events = []
        with patch("youtube_watchlater_tidy.llm_annotation.chat", side_effect=fake_chat):
            result = annotate(
                self.provider,
                self.prompt,
                videos,
                batch_size=4,
                progress=events.append,
            )

        self.assertEqual(calls, [4, 2, 1, 1, 2, 1, 1])
        self.assertEqual(
            [item.video_id for item in result.annotations],
            [video.video_id for video in videos],
        )
        self.assertEqual(len(result.batches), 1)
        self.assertEqual(result.batches[0].usage["prompt_tokens"], 7)
        self.assertEqual(result.batches[0].usage["completion_tokens"], 14)
        self.assertEqual(
            result.batches[0].usage["prompt_tokens_details"]["cached_tokens"],
            7,
        )
        split_messages = [
            event.detail or ""
            for event in events
            if event.kind == "message" and "output limit" in (event.detail or "")
        ]
        self.assertTrue(any("4 videos as 2 + 2" in message for message in split_messages))
        self.assertTrue(any("2 videos as 1 + 1" in message for message in split_messages))
        self.assertEqual(events[-1].kind, "finish")
        self.assertEqual(events[-1].completed, 4)

    def test_content_filter_splits_batch_and_marks_isolated_video_unclear(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=False,
            )

        self.assertEqual(len(videos), 2)
        filtered_id = videos[1].video_id
        calls: list[list[str]] = []

        def fake_chat(provider, messages, json_schema=None, **kwargs):
            batch = json.loads(messages[-1]["content"].split("\n", 1)[1])["videos"]
            video_ids = [row["video_id"] for row in batch]
            calls.append(video_ids)
            usage = {"prompt_tokens": len(calls)}

            if filtered_id in video_ids:
                return ChatResponse(
                    content="",
                    usage=usage,
                    model="model-a",
                    raw={},
                    finish_reason="content_filter",
                )

            video_id = video_ids[0]
            return ChatResponse(
                content=json.dumps(
                    {
                        "annotations": [
                            {
                                "video_id": video_id,
                                "primary_category": "Retrocomputing",
                                "subject": "subject " + video_id,
                                "tags": ["retrocomputing"],
                                "content_type": "technical",
                                "confidence": 0.8,
                            }
                        ]
                    }
                ),
                usage=usage,
                model="model-a",
                raw={},
                finish_reason="stop",
            )

        events = []
        with patch("youtube_watchlater_tidy.llm_annotation.chat", side_effect=fake_chat):
            result = annotate(
                self.provider,
                self.prompt,
                videos,
                batch_size=2,
                progress=events.append,
            )

        self.assertEqual(
            calls,
            [
                [videos[0].video_id, videos[1].video_id],
                [videos[0].video_id],
                [videos[1].video_id],
            ],
        )
        self.assertEqual([item.video_id for item in result.annotations], [v.video_id for v in videos])
        self.assertEqual(result.annotations[0].primary_category, "Retrocomputing")
        fallback = result.annotations[1]
        self.assertEqual(fallback.video_id, filtered_id)
        self.assertEqual(fallback.primary_category, "Unclear")
        self.assertEqual(fallback.tags, ("content-filtered",))
        self.assertEqual(fallback.content_type, "unclassified")
        self.assertEqual(fallback.confidence, 0.0)
        self.assertIn("manual review required", fallback.subject)
        self.assertEqual(result.batches[0].usage["prompt_tokens"], 6)

        messages = [event.detail or "" for event in events if event.kind == "message"]
        self.assertTrue(
            any(
                "provider content filter; retrying 2 videos as 1 + 1" in message
                for message in messages
            )
        )
        self.assertTrue(
            any("recording Unclear fallback for manual review" in message for message in messages)
        )
        self.assertEqual(events[-1].kind, "finish")
        self.assertEqual(events[-1].completed, 2)

    def test_single_video_annotation_truncation_is_actionable(self) -> None:
        video = ClassificationEvidence(
            video_id="video00000A",
            playlist_position=1,
            original_title="A",
            recovered_title=None,
            recovered_source=None,
            dearrow_title=None,
            channel="One",
            channel_id="UCONE",
            duration=1,
            view_count=1,
            upload_date=None,
            availability=None,
        )

        def fake_chat(provider, messages, json_schema=None, **kwargs):
            return ChatResponse(
                content="{",
                usage={},
                model="model-a",
                raw={},
                finish_reason="length",
            )

        with patch("youtube_watchlater_tidy.llm_annotation.chat", side_effect=fake_chat):
            with self.assertRaisesRegex(
                RuntimeError,
                r"single-video annotation response.*increase providers\.test\.max_tokens",
            ):
                annotate(self.provider, self.prompt, [video], batch_size=1)

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

    def test_discovered_taxonomy_store_round_trip(self) -> None:
        discovery = TaxonomyDiscovery(
            categories=self.categories,
            input_sha256="taxonomy-input",
            prompt_sha256="taxonomy-prompt",
            usage={"prompt_tokens": 20},
            response_model="model-a",
        )
        with open_catalogue(self.db_path) as conn:
            taxonomy_id = store_taxonomy(
                conn,
                snapshot_id=self.snapshot,
                selection_id=None,
                provider=self.provider,
                discovery=discovery,
                sample_count=250,
                max_categories=20,
                interest_profile="default",
            )
            payload = taxonomy_payload(conn, taxonomy_id)
            categories = taxonomy_categories(conn, taxonomy_id)
            listing = taxonomy_list_payload(conn, snapshot_id=self.snapshot)

        self.assertEqual(payload["taxonomy_id"], taxonomy_id)
        self.assertEqual(payload["snapshot_id"], self.snapshot)
        self.assertEqual(payload["sample_count"], 250)
        self.assertEqual(payload["max_categories"], 20)
        self.assertEqual(payload["categories"], self.categories)
        self.assertEqual(categories, self.categories)
        self.assertEqual(listing[0]["taxonomy_id"], taxonomy_id)
        self.assertEqual(listing[0]["category_count"], len(self.categories))

    def test_schema_v8_migrates_saved_taxonomy_table(self) -> None:
        path = self.root / "v8.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA user_version = 8")
        ensure_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("llm_taxonomies", tables)

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

    def test_annotation_batches_are_checkpointed_before_run_completion(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
                limit=2,
            )
            run_id = begin_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                taxonomy_source="configured",
                context={"scope": "all", "batch_size": 1},
            )
            first = AnnotationBatchResult(
                annotations=(self._annotation(videos[0].video_id),),
                input_sha256=evidence_hash([videos[0]]),
                usage={"prompt_tokens": 10},
                response_model="model-a",
            )
            store_annotation_batch(
                conn,
                run_id=run_id,
                batch_index=0,
                videos=[videos[0]],
                result=first,
            )

            payload = annotation_run_payload(conn, run_id)
            provider_sha, input_sha, _ = annotation_cache_key(
                self.provider,
                self.prompt,
                videos,
            )
            resumable = incomplete_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                requested_model=self.provider.model,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
                videos=videos,
                batch_size=1,
            )

            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["video_count"], 2)
            self.assertEqual(len(payload["annotations"]), 1)
            self.assertEqual(annotation_completed_batch_indexes(conn, run_id), {0})
            self.assertEqual(resumable, run_id)
            self.assertIsNone(
                cached_annotation_run_id(
                    conn,
                    snapshot_id=self.snapshot,
                    provider_sha256=provider_sha,
                    prompt_sha256=self.prompt.sha256,
                    input_sha256=input_sha,
                )
            )
            with self.assertRaisesRegex(ValueError, "1/2 annotations"):
                complete_annotation_run(conn, run_id)

            second = AnnotationBatchResult(
                annotations=(self._annotation(videos[1].video_id, "Telecoms"),),
                input_sha256=evidence_hash([videos[1]]),
                usage={"prompt_tokens": 11},
                response_model="model-a",
            )
            alternate_provider = ProviderConfig(
                name="alternate",
                preset="generic",
                base_url="https://alternate.invalid/v1",
                model="model-b",
                concurrency=1,
            )
            store_annotation_batch(
                conn,
                run_id=run_id,
                batch_index=1,
                videos=[videos[1]],
                result=second,
                provider=alternate_provider,
            )
            complete_annotation_run(conn, run_id)
            payload = annotation_run_payload(conn, run_id)

        self.assertEqual(payload["status"], "complete")
        self.assertEqual(len(payload["annotations"]), 2)
        self.assertEqual(payload["batches"][0]["provider"], self.provider.name)
        self.assertEqual(payload["batches"][0]["requested_model"], self.provider.model)
        self.assertEqual(payload["batches"][1]["provider"], "alternate")
        self.assertEqual(payload["batches"][1]["requested_model"], "model-b")

    def test_incomplete_resume_ignores_execution_only_provider_changes(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
                limit=2,
            )
            run_id = begin_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                taxonomy_source="configured",
                context={"scope": "all", "batch_size": 1},
            )
            first = AnnotationBatchResult(
                annotations=(self._annotation(videos[0].video_id),),
                input_sha256=evidence_hash([videos[0]]),
                usage={},
                response_model=self.provider.model,
            )
            store_annotation_batch(
                conn,
                run_id=run_id,
                batch_index=0,
                videos=[videos[0]],
                result=first,
            )

            changed_execution = ProviderConfig(
                name=self.provider.name,
                preset=self.provider.preset,
                base_url=self.provider.base_url,
                model=self.provider.model,
                concurrency=self.provider.concurrency,
                max_tokens=self.provider.max_tokens * 2,
                stream=not self.provider.stream,
                structured_mode=self.provider.structured_mode,
            )
            _provider_sha, input_sha, _cache_key = annotation_cache_key(
                changed_execution,
                self.prompt,
                videos,
            )
            resumed = incomplete_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                requested_model=changed_execution.model,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
                videos=videos,
                batch_size=1,
            )

        self.assertEqual(resumed, run_id)

    def test_incomplete_resume_can_ignore_different_provider_and_model(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
                limit=1,
            )
            run_id = begin_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                taxonomy_source="configured",
            )
            changed_provider = ProviderConfig(
                name="alternate",
                preset="generic",
                base_url="https://alternate.invalid/v1",
                model="model-b",
                concurrency=1,
            )
            _provider_sha, input_sha, _cache_key = annotation_cache_key(
                changed_provider,
                self.prompt,
                videos,
            )
            portable_resume = incomplete_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                requested_model=None,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
                videos=videos,
                batch_size=1,
            )
            strict_resume = incomplete_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                requested_model=changed_provider.model,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
                videos=videos,
                batch_size=1,
            )

        self.assertEqual(portable_resume, run_id)
        self.assertIsNone(strict_resume)

    def test_semantic_complete_run_is_reusable_across_provider_model_changes(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
                limit=1,
            )
            result = AnnotationRunResult(
                annotations=(self._annotation(videos[0].video_id),),
                batches=(
                    AnnotationBatchResult(
                        annotations=(self._annotation(videos[0].video_id),),
                        input_sha256=evidence_hash(videos),
                        usage={},
                        response_model=self.provider.model,
                    ),
                ),
            )
            run_id = store_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                result=result,
                taxonomy_source="configured",
                context={"stage": "semantic_annotation"},
            )
            changed_provider = ProviderConfig(
                name="alternate",
                preset="generic",
                base_url="https://alternate.invalid/v1",
                model="model-b",
                concurrency=1,
            )
            changed_sha, input_sha, _cache_key = annotation_cache_key(
                changed_provider,
                self.prompt,
                videos,
            )
            exact = cached_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                provider_sha256=changed_sha,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
                required_context={"stage": "semantic_annotation"},
            )
            reusable = reusable_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
                required_context={"stage": "semantic_annotation"},
            )

        self.assertIsNone(exact)
        self.assertEqual(reusable, run_id)

    def test_incomplete_resume_prefers_most_progressed_compatible_run(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
                limit=2,
            )
            progressed_run = begin_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=self.provider,
                prompt=self.prompt,
                videos=videos,
                taxonomy_source="configured",
                context={"stage": "semantic_annotation"},
            )
            first = AnnotationBatchResult(
                annotations=(self._annotation(videos[0].video_id),),
                input_sha256=evidence_hash([videos[0]]),
                usage={},
                response_model=self.provider.model,
            )
            store_annotation_batch(
                conn,
                run_id=progressed_run,
                batch_index=0,
                videos=[videos[0]],
                result=first,
                provider=self.provider,
            )
            newer_empty_run = begin_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=ProviderConfig(
                    name="alternate",
                    preset="generic",
                    base_url="https://alternate.invalid/v1",
                    model="model-b",
                    concurrency=1,
                ),
                prompt=self.prompt,
                videos=videos,
                taxonomy_source="configured",
                context={"stage": "semantic_annotation"},
            )
            self.assertGreater(newer_empty_run, progressed_run)
            _provider_sha, input_sha, _cache_key = annotation_cache_key(
                self.provider,
                self.prompt,
                videos,
            )
            resumed = incomplete_annotation_run_id(
                conn,
                snapshot_id=self.snapshot,
                requested_model=None,
                prompt_sha256=self.prompt.sha256,
                input_sha256=input_sha,
                videos=videos,
                batch_size=1,
                required_context={"stage": "semantic_annotation"},
            )

        self.assertEqual(resumed, progressed_run)

    def test_annotation_resume_skips_checkpointed_batches(self) -> None:
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
        requested: list[str] = []
        checkpointed: list[int] = []

        def fake_chat(provider, messages, json_schema=None, **kwargs):
            batch = json.loads(messages[-1]["content"].split("\n", 1)[1])["videos"]
            requested.extend(row["video_id"] for row in batch)
            video_id = batch[0]["video_id"]
            return ChatResponse(
                content=json.dumps(
                    {
                        "annotations": [
                            {
                                "video_id": video_id,
                                "primary_category": "Telecoms",
                                "subject": "subject",
                                "tags": ["telecoms"],
                                "content_type": "technical",
                                "confidence": 0.8,
                            }
                        ]
                    }
                ),
                usage={},
                model="model-a",
                raw={},
                finish_reason="stop",
            )

        events = []
        with patch("youtube_watchlater_tidy.llm_annotation.chat", side_effect=fake_chat):
            result = annotate(
                self.provider,
                self.prompt,
                videos,
                batch_size=1,
                completed_batch_indexes={0},
                on_batch=lambda index, batch, batch_result: checkpointed.append(index),
                progress=events.append,
            )

        self.assertEqual(requested, ["video00000E"])
        self.assertEqual(checkpointed, [1])
        self.assertEqual([item.video_id for item in result.annotations], ["video00000E"])
        self.assertEqual(events[0].completed, 1)
        self.assertEqual(events[-1].completed, 2)

    def test_failed_later_response_preserves_earlier_checkpoint(self) -> None:
        with open_catalogue(self.db_path) as conn:
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
                limit=2,
            )
            provider = ProviderConfig(
                name="test",
                preset="generic",
                base_url="https://example.invalid/v1",
                model="model-a",
                concurrency=1,
            )
            run_id = begin_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=provider,
                prompt=self.prompt,
                videos=videos,
                taxonomy_source="configured",
                context={"scope": "all", "batch_size": 1},
            )
            calls = 0

            def fake_chat(provider, messages, json_schema=None, **kwargs):
                nonlocal calls
                calls += 1
                batch = json.loads(messages[-1]["content"].split("\n", 1)[1])["videos"]
                if calls == 2:
                    return ChatResponse(
                        content="not json",
                        usage={},
                        model="model-a",
                        raw={},
                        finish_reason="stop",
                    )
                video_id = batch[0]["video_id"]
                return ChatResponse(
                    content=json.dumps(
                        {
                            "annotations": [
                                {
                                    "video_id": video_id,
                                    "primary_category": "Retrocomputing",
                                    "subject": "subject",
                                    "tags": ["retrocomputing"],
                                    "content_type": "technical",
                                    "confidence": 0.8,
                                }
                            ]
                        }
                    ),
                    usage={},
                    model="model-a",
                    raw={},
                    finish_reason="stop",
                )

            def checkpoint(index, batch, batch_result):
                store_annotation_batch(
                    conn,
                    run_id=run_id,
                    batch_index=index,
                    videos=batch,
                    result=batch_result,
                )

            with patch("youtube_watchlater_tidy.llm_annotation.chat", side_effect=fake_chat):
                with self.assertRaisesRegex(RuntimeError, "non-JSON"):
                    annotate(
                        provider,
                        self.prompt,
                        videos,
                        batch_size=1,
                        on_batch=checkpoint,
                    )

            payload = annotation_run_payload(conn, run_id)

        self.assertEqual(payload["status"], "error")
        self.assertEqual(len(payload["annotations"]), 1)
        self.assertEqual(payload["annotations"][0]["video_id"], videos[0].video_id)

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
