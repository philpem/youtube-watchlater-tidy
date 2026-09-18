from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_annotation import (
    AnnotationBatchResult,
    AnnotationPrompt,
    AnnotationRunResult,
    SemanticAnnotation,
)
from youtube_watchlater_tidy.llm_annotation_store import (
    annotation_run_payload,
    content_filtered_retry_target,
    store_annotation_run,
)
from youtube_watchlater_tidy.llm_classification import (
    classification_evidence,
    evidence_hash,
)
from youtube_watchlater_tidy.llm_cli import main as llm_main
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.llm_provider import ChatResponse


class LLMContentFilterRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalogue.sqlite3"
        self.config_path = self.root / "watchlater.toml"
        self.config_path.write_text(
            """
default_provider = "source"

[providers.source]
preset = "generic"
base_url = "https://source.invalid/v1"
model = "source-model"
concurrency = 1

[providers.alternate]
preset = "generic"
base_url = "https://alternate.invalid/v1"
model = "alternate-model"
concurrency = 1
""".lstrip(),
            encoding="utf-8",
        )
        source = self.root / "watch-later.json"
        source.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "entries": [
                        {
                            "id": "video00000A",
                            "title": "Ordinary retrocomputing video",
                            "channel_id": "UCRETRO",
                            "channel": "Retro Lab",
                        },
                        {
                            "id": "video00000B",
                            "title": "Video that triggered a provider filter",
                            "channel_id": "UCFILTER",
                            "channel": "Filter Test",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            videos = classification_evidence(
                conn,
                self.snapshot,
                include_decided=True,
            )
            self.filtered_video = videos[1]
            provider = ProviderConfig(
                name="source",
                preset="generic",
                base_url="https://source.invalid/v1",
                model="source-model",
                concurrency=1,
            )
            prompt = AnnotationPrompt(
                system="system",
                interest_brief="",
                profile_name=None,
                categories={
                    "Retrocomputing": "Historic computers.",
                    "Other": "Outside the main vocabulary.",
                    "Unclear": "Insufficient evidence.",
                },
                sha256="source-prompt",
            )
            annotations = (
                SemanticAnnotation(
                    video_id=videos[0].video_id,
                    primary_category="Retrocomputing",
                    subject="retrocomputing",
                    tags=("retrocomputing",),
                    content_type="technical",
                    confidence=0.9,
                ),
                SemanticAnnotation(
                    video_id=videos[1].video_id,
                    primary_category="Unclear",
                    subject=(
                        "Provider content filter prevented semantic annotation; "
                        "manual review required"
                    ),
                    tags=("content-filtered",),
                    content_type="unclassified",
                    confidence=0.0,
                ),
            )
            batch = AnnotationBatchResult(
                annotations=annotations,
                input_sha256=evidence_hash(videos),
                usage={},
                response_model="source-model",
            )
            self.source_run_id = store_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=provider,
                prompt=prompt,
                videos=videos,
                result=AnnotationRunResult(
                    annotations=annotations,
                    batches=(batch,),
                ),
                taxonomy_source="configured",
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _base_args(self) -> list[str]:
        return [
            "--config",
            str(self.config_path),
            "--db",
            str(self.db_path),
        ]

    def test_retry_target_uses_only_stored_content_filtered_evidence(self) -> None:
        with open_catalogue(self.db_path) as conn:
            target = content_filtered_retry_target(conn, self.source_run_id)

        self.assertEqual(target.source_run_id, self.source_run_id)
        self.assertEqual(target.snapshot_id, self.snapshot)
        self.assertEqual(len(target.videos), 1)
        self.assertEqual(target.videos[0], self.filtered_video)
        self.assertIn("Unclear", target.taxonomy)

    def test_retry_dry_run_supports_provider_and_model_override(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = llm_main(
                self._base_args()
                + [
                    "retry-content-filtered",
                    "--run-id",
                    str(self.source_run_id),
                    "--provider",
                    "alternate",
                    "--model",
                    "alternate-model-v2",
                    "--dry-run",
                ]
            )

        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["stage"], "content_filter_retry")
        self.assertEqual(payload["source_run_id"], self.source_run_id)
        self.assertEqual(payload["provider"], "alternate")
        self.assertEqual(payload["model"], "alternate-model-v2")
        self.assertEqual(payload["video_count"], 1)
        self.assertEqual(payload["videos"][0]["video_id"], self.filtered_video.video_id)

    def test_retry_stores_new_run_and_preserves_source_run(self) -> None:
        def fake_chat(provider, messages, json_schema=None, **kwargs):
            self.assertEqual(provider.name, "alternate")
            self.assertEqual(provider.model, "alternate-model")
            batch = json.loads(messages[-1]["content"].split("\n", 1)[1])["videos"]
            self.assertEqual([row["video_id"] for row in batch], [self.filtered_video.video_id])
            return ChatResponse(
                content=json.dumps(
                    {
                        "annotations": [
                            {
                                "video_id": self.filtered_video.video_id,
                                "primary_category": "Retrocomputing",
                                "subject": "Recovered semantic annotation",
                                "tags": ["retrocomputing"],
                                "content_type": "technical",
                                "confidence": 0.8,
                            }
                        ]
                    }
                ),
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                model="alternate-model",
                raw={},
                finish_reason="stop",
            )

        stdout = io.StringIO()
        with patch(
            "youtube_watchlater_tidy.llm_annotation.chat",
            side_effect=fake_chat,
        ), redirect_stdout(stdout):
            rc = llm_main(
                self._base_args()
                + [
                    "retry-content-filtered",
                    "--run-id",
                    str(self.source_run_id),
                    "--provider",
                    "alternate",
                    "--batch-size",
                    "1",
                    "--progress",
                    "never",
                ]
            )

        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["source_run_id"], self.source_run_id)
        self.assertNotEqual(payload["run_id"], self.source_run_id)
        self.assertEqual(payload["provider"], "alternate")
        self.assertEqual(payload["configured_model"], "alternate-model")
        self.assertEqual(payload["video_count"], 1)
        self.assertEqual(payload["annotations"][0]["primary_category"], "Retrocomputing")

        with open_catalogue(self.db_path) as conn:
            source_payload = annotation_run_payload(conn, self.source_run_id)
            retry_payload = annotation_run_payload(conn, payload["run_id"])

        self.assertEqual(source_payload["annotations"][1]["tags"], ["content-filtered"])
        self.assertEqual(retry_payload["context"]["stage"], "content_filter_retry")
        self.assertEqual(retry_payload["context"]["source_run_id"], self.source_run_id)


if __name__ == "__main__":
    unittest.main()
