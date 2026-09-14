from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.db import open_catalogue
from youtube_watchlater_tidy.importer import import_watchlater_json
from youtube_watchlater_tidy.recovery import (
    _fetch_findyoutubevideo_stream,
    recover_with_findyoutubevideo,
)


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)


class RecoveryStreamTests(unittest.TestCase):
    def test_stream_is_reconstructed_and_reports_service_progress(self) -> None:
        video_id = "dQw4w9WgXcQ"
        wayback_link = {
            "type": "link",
            "classname": "WaybackMachine",
            "url": "https://web.archive.org/web/20200101000000/https://youtube.com/watch?v=dQw4w9WgXcQ",
            "contains": {"metadata": True},
            "title": "Watch page",
            "note": None,
        }
        lines = [
            json.dumps(
                {
                    "WaybackMachine": "Wayback Machine",
                    "GhostArchive": "GhostArchive",
                }
            ).encode() + b"\n",
            json.dumps(wayback_link).encode() + b"\n",
            json.dumps(
                {
                    "type": "service",
                    "classname": "WaybackMachine",
                    "name": "Wayback Machine",
                    "archived": True,
                    "available": [wayback_link],
                    "error": None,
                    "lastupdated": 1,
                    "note": "",
                    "rawraw": None,
                    "metaonly": True,
                    "comments": False,
                    "maybe_paywalled": False,
                }
            ).encode() + b"\n",
            json.dumps(
                {
                    "type": "service",
                    "classname": "GhostArchive",
                    "name": "GhostArchive",
                    "archived": False,
                    "available": [],
                    "error": None,
                    "lastupdated": 2,
                    "note": "",
                    "rawraw": 404,
                    "metaonly": False,
                    "comments": False,
                    "maybe_paywalled": False,
                }
            ).encode() + b"\n",
            b"null\n",
            json.dumps(
                {
                    "video": False,
                    "metaonly": True,
                    "comments": False,
                    "human_friendly": "Archived with metadata only.",
                }
            ).encode() + b"\n",
        ]
        progress: list[tuple[str, str]] = []

        with patch(
            "youtube_watchlater_tidy.recovery.urlopen",
            return_value=_FakeResponse(lines),
        ) as mocked_urlopen:
            result = _fetch_findyoutubevideo_stream(
                video_id,
                on_service=lambda vid, service: progress.append((vid, service)),
            )

        request = mocked_urlopen.call_args.args[0]
        self.assertIn("stream=true", request.full_url)
        self.assertIn("includeRaw=true", request.full_url)
        self.assertEqual(result["id"], video_id)
        self.assertEqual(result["verdict"]["metaonly"], True)
        self.assertEqual(len(result["keys"]), 2)
        self.assertEqual(result["keys"][0]["available"][0]["url"], wayback_link["url"])
        self.assertEqual(
            progress,
            [(video_id, "Wayback Machine"), (video_id, "GhostArchive")],
        )

    def test_parallel_finder_fetches_do_not_share_sqlite_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "watch-later.json"
            source.write_text(
                json.dumps(
                    {
                        "id": "WL",
                        "entries": [
                            {"id": f"deleted0000{i}", "title": "[Deleted video]"}
                            for i in range(4)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            db_path = root / "catalogue.sqlite3"
            with open_catalogue(db_path) as conn:
                snapshot = import_watchlater_json(conn, source).snapshot_id
                video_ids = [
                    str(row["video_id"])
                    for row in conn.execute(
                        "SELECT video_id FROM snapshot_entries WHERE snapshot_id = ? ORDER BY position",
                        (snapshot,),
                    )
                ]

                lock = threading.Lock()
                release = threading.Event()
                active = 0
                max_active = 0

                def fake_stream(video_id: str, **kwargs):
                    nonlocal active, max_active
                    with lock:
                        active += 1
                        max_active = max(max_active, active)
                        if active >= 3:
                            release.set()
                    release.wait(timeout=2)
                    with lock:
                        active -= 1
                    return {
                        "id": video_id,
                        "status": "ok",
                        "api_version": 5,
                        "keys": [],
                        "verdict": {
                            "video": False,
                            "metaonly": False,
                            "comments": False,
                            "human_friendly": "Video not found.",
                        },
                    }

                with patch(
                    "youtube_watchlater_tidy.recovery._fetch_findyoutubevideo_stream",
                    side_effect=fake_stream,
                ):
                    result = recover_with_findyoutubevideo(
                        conn,
                        video_ids,
                        workers=3,
                        show_progress=False,
                    )

                self.assertGreaterEqual(max_active, 3)
                self.assertEqual(result.attempted, 4)
                self.assertEqual(result.not_found, 4)
                self.assertEqual(result.failed, 0)
                stored = conn.execute("SELECT COUNT(*) FROM archive_lookups").fetchone()[0]
                self.assertEqual(stored, 4)

    def test_workers_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalogue.sqlite3"
            with open_catalogue(db_path) as conn:
                with self.assertRaisesRegex(ValueError, "workers"):
                    recover_with_findyoutubevideo(
                        conn,
                        [],
                        workers=0,
                        show_progress=False,
                    )


if __name__ == "__main__":
    unittest.main()
