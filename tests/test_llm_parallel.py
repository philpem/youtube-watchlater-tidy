from __future__ import annotations

import os
import signal
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

    @unittest.skipUnless(hasattr(signal, "SIGINT"), "SIGINT is required")
    def test_first_sigint_drains_active_requests_without_starting_queued_work(self) -> None:
        both_started = threading.Event()
        drain_reported = threading.Event()
        started: list[int] = []
        consumed: list[int] = []
        reports: list[tuple[int, int]] = []
        lock = threading.Lock()

        def request(index: int, item: int) -> str:
            with lock:
                started.append(index)
                if len(started) == 2:
                    both_started.set()
            self.assertTrue(both_started.wait(1.0))
            if index == 0:
                os.kill(os.getpid(), signal.SIGINT)
            self.assertTrue(drain_reported.wait(1.0))
            return f"response-{index}"

        def consume(index: int, item: int, response: str) -> str:
            consumed.append(index)
            return response

        def on_interrupt(in_flight: int, queued: int) -> None:
            reports.append((in_flight, queued))
            drain_reported.set()

        with self.assertRaises(KeyboardInterrupt):
            run_bounded_parallel(
                list(range(5)),
                concurrency=2,
                request=request,
                consume=consume,
                drain_on_interrupt=True,
                on_interrupt=on_interrupt,
            )

        self.assertEqual(set(started), {0, 1})
        self.assertEqual(set(consumed), {0, 1})
        self.assertEqual(reports, [(2, 3)])

    @unittest.skipUnless(hasattr(signal, "SIGINT"), "SIGINT is required")
    def test_second_sigint_forces_immediate_abort_while_draining(self) -> None:
        both_started = threading.Event()
        drain_reported = threading.Event()
        release = threading.Event()
        started: list[int] = []
        consumed: list[int] = []
        lock = threading.Lock()

        def request(index: int, item: int) -> str:
            with lock:
                started.append(index)
                if len(started) == 2:
                    both_started.set()
            self.assertTrue(both_started.wait(1.0))
            if index == 0:
                os.kill(os.getpid(), signal.SIGINT)
                self.assertTrue(drain_reported.wait(1.0))
                os.kill(os.getpid(), signal.SIGINT)
            release.wait(1.0)
            return f"response-{index}"

        def consume(index: int, item: int, response: str) -> str:
            consumed.append(index)
            return response

        def on_interrupt(in_flight: int, queued: int) -> None:
            drain_reported.set()

        try:
            with self.assertRaises(KeyboardInterrupt):
                run_bounded_parallel(
                    list(range(5)),
                    concurrency=2,
                    request=request,
                    consume=consume,
                    drain_on_interrupt=True,
                    on_interrupt=on_interrupt,
                )
            self.assertEqual(set(started), {0, 1})
            self.assertEqual(consumed, [])
            self.assertTrue(drain_reported.is_set())
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
