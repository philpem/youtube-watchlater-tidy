from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from youtube_watchlater_tidy.llm_cli import _print_stored_run_summary


class LLMCLIOutputTests(unittest.TestCase):
    def _render(self, output: dict, *, kind: str, command: str) -> str:
        stream = io.StringIO()
        with redirect_stdout(stream):
            _print_stored_run_summary(
                output,
                kind=kind,
                results_command=command,
            )
        return stream.getvalue()

    def test_annotation_summary_does_not_dump_annotations_or_batches(self) -> None:
        output = {
            "run_id": 5,
            "status": "complete",
            "stored_video_count": 3925,
            "batch_count": 393,
            "cache": "resume",
            "annotations": [{"video_id": "abc", "subject": "very large payload"}],
            "batches": [{"batch_index": 0, "validated_json": "very large payload"}],
        }
        rendered = self._render(
            output,
            kind="Annotation",
            command="watchlater-llm annotation-results",
        )
        self.assertEqual(
            rendered,
            "Annotation run 5: complete; 3925 video(s), 393 batch(es), cache=resume. "
            "Full results: watchlater-llm annotation-results --run-id 5\n",
        )
        self.assertNotIn("annotations", rendered)
        self.assertNotIn("very large payload", rendered)

    def test_classification_summary_includes_retry_metadata_without_payload(self) -> None:
        output = {
            "run_id": 12,
            "status": "complete",
            "video_count": 50,
            "cache": "hit",
            "source_run_id": 7,
            "missing_description_video_ids": ["a", "b"],
            "classifications": [{"video_id": "a", "reason": "large payload"}],
            "batches": [{}, {}, {}, {}, {}],
        }
        rendered = self._render(
            output,
            kind="Description refinement",
            command="watchlater-llm results",
        )
        self.assertIn("50 video(s), 5 batch(es), cache=hit", rendered)
        self.assertIn("source_run=7", rendered)
        self.assertIn("2 description(s) still missing", rendered)
        self.assertIn("watchlater-llm results --run-id 12", rendered)
        self.assertNotIn("large payload", rendered)


if __name__ == "__main__":
    unittest.main()
