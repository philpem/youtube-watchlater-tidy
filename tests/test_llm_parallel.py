from __future__ import annotations

import threading
import unittest

from youtube_watchlater_tidy.llm_parallel import run_bounded_parallel


class BoundedParallelTests(unittest.TestCase):
    def test_validation_failure_does_not_start_queued_work(self) -> None:
        release = threading.Event()
        started: list[int] = []

        def request(index: int, item: int) -> str:
            started.append(index)
            if index == 1:
                release.wait(1.0)
            return f"response-{index}"

        def consume(index: int, item: int, response: str) -> str:
            if index == 0:
                raise RuntimeError("invalid response")
            return response

        try:
            with self.assertRaisesRegex(RuntimeError, "invalid response"):
                run_bounded_parallel(
                    list(range(10)),
                    concurrency=2,
                    request=request,
                    consume=consume,
                )
            self.assertEqual(set(started), {0, 1})
        finally:
            release.set()

    def test_keyboard_interrupt_does_not_start_queued_work(self) -> None:
        release = threading.Event()
        started: list[int] = []

        def request(index: int, item: int) -> str:
            started.append(index)
            if index == 1:
                release.wait(1.0)
            return f"response-{index}"

        def consume(index: int, item: int, response: str) -> str:
            if index == 0:
                raise KeyboardInterrupt
            return response

        try:
            with self.assertRaises(KeyboardInterrupt):
                run_bounded_parallel(
                    list(range(10)),
                    concurrency=2,
                    request=request,
                    consume=consume,
                )
            self.assertEqual(set(started), {0, 1})
        finally:
            release.set()

    def test_replacement_request_starts_only_after_successful_consume(self) -> None:
        started: list[int] = []
        consumed: list[int] = []

        def request(index: int, item: int) -> str:
            started.append(index)
            return f"response-{index}"

        def consume(index: int, item: int, response: str) -> str:
            consumed.append(index)
            return response

        results = run_bounded_parallel(
            list(range(5)),
            concurrency=2,
            request=request,
            consume=consume,
        )
        self.assertEqual(set(results), set(range(5)))
        self.assertEqual(set(started), set(range(5)))
        self.assertEqual(set(consumed), set(range(5)))


if __name__ == "__main__":
    unittest.main()
