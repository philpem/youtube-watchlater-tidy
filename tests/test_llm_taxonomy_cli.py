from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.llm_annotation import TaxonomyDiscovery
from youtube_watchlater_tidy.llm_cli import main as llm_main
from youtube_watchlater_tidy.llm_config import ProviderConfig
from youtube_watchlater_tidy.llm_taxonomy_store import (
    store_taxonomy,
    taxonomy_list_payload,
)


class LLMTaxonomyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalogue.sqlite3"
        self.config_path = self.root / "watchlater.toml"
        self.config_path.write_text(
            """
default_provider = "test"

[providers.test]
preset = "generic"
base_url = "https://example.invalid/v1"
model = "model-a"
concurrency = 2
""".lstrip(),
            encoding="utf-8",
        )
        source = self.root / "watch-later.json"
        source.write_text(
            json.dumps(
                {
                    "id": "WL",
                    "entries": [
                        {"id": "video00000A", "title": "Acorn repair", "channel": "Retro Lab"},
                        {"id": "video00000B", "title": "V.34 modem", "channel": "Telecom Lab"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with open_catalogue(self.db_path) as conn:
            self.snapshot = import_watchlater_json(conn, source).snapshot_id

        self.provider = ProviderConfig(
            name="test",
            preset="generic",
            base_url="https://example.invalid/v1",
            model="model-a",
            concurrency=2,
        )
        self.discovery = TaxonomyDiscovery(
            categories={
                "Retrocomputing": "Historic computers.",
                "Telecoms": "Communications systems.",
                "Electronics": "Electronic engineering.",
                "Other": "Other material.",
                "Unclear": "Insufficient evidence.",
            },
            input_sha256="taxonomy-input",
            prompt_sha256="taxonomy-prompt",
            usage={"prompt_tokens": 20},
            response_model="model-a",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _base_args(self) -> list[str]:
        return ["--config", str(self.config_path), "--db", str(self.db_path)]

    def test_discovered_taxonomy_is_saved_before_annotation_failure(self) -> None:
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch(
            "youtube_watchlater_tidy.llm_cli.discover_taxonomy",
            return_value=self.discovery,
        ), patch(
            "youtube_watchlater_tidy.llm_cli.annotate",
            side_effect=RuntimeError("annotation failed"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            rc = llm_main(
                self._base_args()
                + [
                    "annotate",
                    "--scope",
                    "all",
                    "--taxonomy",
                    "discover",
                    "--taxonomy-sample",
                    "2",
                    "--max-categories",
                    "3",
                    "--progress",
                    "never",
                ]
            )

        self.assertEqual(rc, 2)
        self.assertIn("Saved discovered taxonomy", stderr.getvalue())
        with open_catalogue(self.db_path) as conn:
            taxonomies = taxonomy_list_payload(conn)
        self.assertEqual(len(taxonomies), 1)
        self.assertEqual(taxonomies[0]["sample_count"], 2)

    def test_saved_taxonomy_can_be_reused_without_discovery_request(self) -> None:
        with open_catalogue(self.db_path) as conn:
            taxonomy_id = store_taxonomy(
                conn,
                snapshot_id=self.snapshot,
                selection_id=None,
                provider=self.provider,
                discovery=self.discovery,
                sample_count=2,
                max_categories=3,
                interest_profile=None,
            )

        stdout = io.StringIO()
        with patch("youtube_watchlater_tidy.llm_cli.discover_taxonomy") as discover, redirect_stdout(
            stdout
        ):
            rc = llm_main(
                self._base_args()
                + [
                    "annotate",
                    "--scope",
                    "all",
                    "--taxonomy",
                    "saved",
                    "--taxonomy-id",
                    str(taxonomy_id),
                    "--dry-run",
                ]
            )

        self.assertEqual(rc, 0)
        discover.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["taxonomy_mode"], "saved")
        self.assertEqual(payload["taxonomy_id"], taxonomy_id)
        self.assertEqual(payload["taxonomy"]["Retrocomputing"], "Historic computers.")

    def test_taxonomy_commands_list_and_show_saved_vocabularies(self) -> None:
        with open_catalogue(self.db_path) as conn:
            taxonomy_id = store_taxonomy(
                conn,
                snapshot_id=self.snapshot,
                selection_id=None,
                provider=self.provider,
                discovery=self.discovery,
                sample_count=2,
                max_categories=3,
                interest_profile=None,
            )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = llm_main(self._base_args() + ["taxonomies"])
        self.assertEqual(rc, 0)
        listing = json.loads(stdout.getvalue())
        self.assertEqual(listing[0]["taxonomy_id"], taxonomy_id)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = llm_main(
                self._base_args()
                + ["taxonomy", "--taxonomy-id", str(taxonomy_id)]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["taxonomy_id"], taxonomy_id)
        self.assertIn("Telecoms", payload["categories"])


if __name__ == "__main__":
    unittest.main()
