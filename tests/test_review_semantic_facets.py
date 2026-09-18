from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_annotation import (
    AnnotationBatchResult,
    AnnotationPrompt,
    AnnotationRunResult,
    SemanticAnnotation,
)
from youtube_watchlater_tidy.llm_annotation_store import store_annotation_run
from youtube_watchlater_tidy.llm_classification import ClassificationEvidence
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.review_report import render_review_html, review_rows
from youtube_watchlater_tidy.triage import apply_selection_action, select_title


class ReviewSemanticFacetTests(unittest.TestCase):
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
                            "title": "Repairing an Acorn Archimedes",
                            "channel_id": "UCRETRO",
                            "channel": "Retro Lab",
                            "duration": 600,
                            "view_count": 100,
                        }
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
        prompt = AnnotationPrompt(
            system="system",
            interest_brief="",
            profile_name=None,
            categories={
                "Retrocomputing": "Historic computers.",
                "Other": "Other material.",
                "Unclear": "Insufficient evidence.",
            },
            sha256="annotation-prompt",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id
            selection = select_title(conn, contains="Acorn", snapshot_id=self.snapshot)
            apply_selection_action(conn, "keep", selection_id=selection.selection_id)
            evidence = ClassificationEvidence(
                video_id="video00000A",
                playlist_position=1,
                original_title="Repairing an Acorn Archimedes",
                recovered_title=None,
                recovered_source=None,
                dearrow_title=None,
                channel="Retro Lab",
                channel_id="UCRETRO",
                duration=600,
                view_count=100,
                upload_date=None,
                availability=None,
            )
            annotation = SemanticAnnotation(
                video_id="video00000A",
                primary_category="Retrocomputing",
                subject="Acorn Archimedes hardware repair",
                tags=("acorn", "archimedes", "hardware repair"),
                content_type="repair",
                confidence=0.94,
            )
            self.run_id = store_annotation_run(
                conn,
                snapshot_id=self.snapshot,
                provider=provider,
                prompt=prompt,
                videos=[evidence],
                result=AnnotationRunResult(
                    annotations=(annotation,),
                    batches=(
                        AnnotationBatchResult(
                            annotations=(annotation,),
                            input_sha256="batch",
                            usage={},
                            response_model="model-a",
                        ),
                    ),
                ),
                taxonomy_source="configured",
                context={"scope": "all"},
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_review_rows_keep_annotation_separate_from_decision(self) -> None:
        with open_catalogue(self.db_path) as conn:
            _, rows = review_rows(conn, self.snapshot)
        row = rows[0]
        self.assertEqual(row["current_decision"]["action"], "keep")
        self.assertIsNone(row["llm"])
        self.assertEqual(row["annotation"]["run_id"], self.run_id)
        self.assertEqual(row["annotation"]["run_status"], "complete")
        self.assertEqual(row["annotation"]["primary_category"], "Retrocomputing")
        self.assertEqual(
            row["annotation"]["tags"],
            ["acorn", "archimedes", "hardware repair"],
        )

    def test_review_rows_show_checkpointed_annotation_from_incomplete_run(self) -> None:
        with open_catalogue(self.db_path) as conn:
            conn.execute(
                "UPDATE llm_annotation_runs SET status = 'error' WHERE id = ?",
                (self.run_id,),
            )
            conn.commit()
            _, rows = review_rows(conn, self.snapshot)

        annotation = rows[0]["annotation"]
        self.assertIsNotNone(annotation)
        self.assertEqual(annotation["run_id"], self.run_id)
        self.assertEqual(annotation["run_status"], "error")
        self.assertEqual(annotation["primary_category"], "Retrocomputing")

    def test_html_exposes_clickable_semantic_facets(self) -> None:
        with open_catalogue(self.db_path) as conn:
            snapshot, rows = review_rows(conn, self.snapshot)
        page = render_review_html(snapshot, rows)
        self.assertIn('id="categoryFacets"', page)
        self.assertIn('id="tagFacets"', page)
        self.assertIn('id="clearSemanticFilters"', page)
        self.assertIn("Retrocomputing", page)
        self.assertIn("hardware repair", page)
        self.assertIn("function matchesSemantic", page)
        self.assertIn("selectedCategories", page)
        self.assertIn("selectedTags", page)
        self.assertIn("renderFacets();", page)
        self.assertIn("visible.slice(start,start+pageSize)", page)


if __name__ == "__main__":
    unittest.main()
