from __future__ import annotations

import argparse
import io
import unittest

from youtube_watchlater_tidy.progress import (
    ConsoleProgress,
    ProgressEvent,
    legacy_message_callback,
    selected_progress_mode,
)


class ProgressTests(unittest.TestCase):
    def test_never_mode_is_silent(self) -> None:
        stream = io.StringIO()
        progress = ConsoleProgress("never", stream=stream)
        progress(ProgressEvent("Work", kind="start", completed=0, total=10))
        progress(ProgressEvent("Work", kind="update", completed=5, total=10))
        progress(ProgressEvent("Work", kind="finish", completed=10, total=10))
        progress.close()
        self.assertEqual(stream.getvalue(), "")

    def test_auto_redirect_uses_plain_stderr_lines(self) -> None:
        stream = io.StringIO()
        ticks = iter([100.0, 101.0, 102.0, 103.0])
        progress = ConsoleProgress("auto", stream=stream, clock=lambda: next(ticks))
        progress(
            ProgressEvent(
                "LLM classification",
                kind="start",
                completed=0,
                total=20,
                unit="video",
                detail="2 batches",
            )
        )
        progress(
            ProgressEvent(
                "LLM classification",
                kind="update",
                completed=10,
                total=20,
                unit="video",
                detail="batch 1/2",
            )
        )
        progress(
            ProgressEvent(
                "LLM classification",
                kind="finish",
                completed=20,
                total=20,
                unit="video",
                detail="complete",
            )
        )
        text = stream.getvalue()
        self.assertIn("LLM classification: 0/20 video 2 batches", text)
        self.assertIn("LLM classification: 10/20 video batch 1/2", text)
        self.assertIn("LLM classification: 20/20 video complete", text)
        self.assertNotIn("\r", text)

    def test_retry_is_always_visible_when_redirected(self) -> None:
        stream = io.StringIO()
        ticks = iter([100.0, 101.0])
        progress = ConsoleProgress("auto", stream=stream, clock=lambda: next(ticks))
        progress(
            ProgressEvent(
                "LLM annotation",
                kind="retry",
                detail="provider rate limited; retry 2/3 in 1s",
            )
        )
        self.assertIn("retry 2/3", stream.getvalue())

    def test_legacy_status_messages_can_feed_renderer(self) -> None:
        stream = io.StringIO()
        progress = ConsoleProgress("auto", stream=stream, clock=lambda: 100.0)
        callback = legacy_message_callback(progress, "Watch Later removal")
        assert callback is not None
        callback("Scanning Watch Later for 50 planned video(s)")
        self.assertIn("Scanning Watch Later", stream.getvalue())

    def test_no_progress_alias_wins(self) -> None:
        args = argparse.Namespace(progress="always", no_progress=True)
        self.assertEqual(selected_progress_mode(args), "never")


if __name__ == "__main__":
    unittest.main()
