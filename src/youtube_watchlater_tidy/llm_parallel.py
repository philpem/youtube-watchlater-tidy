from __future__ import annotations

import queue
import signal
import threading
from types import FrameType
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")
U = TypeVar("U")
R = TypeVar("R")


def run_bounded_parallel(
    items: Sequence[T],
    *,
    concurrency: int,
    request: Callable[[int, T], U],
    consume: Callable[[int, T, U], R],
    on_response: Callable[[int, T, U], None] | None = None,
    drain_on_interrupt: bool = False,
    on_interrupt: Callable[[int, int], None] | None = None,
) -> dict[int, R]:
    """Run bounded concurrent requests and consume responses on the caller thread.

    Only the configured concurrency may be in flight. A replacement request is not
    started until the previous response has been successfully consumed/validated.

    By default KeyboardInterrupt retains the historical fail-fast behaviour. With
    drain_on_interrupt enabled on the main thread, the first SIGINT stops submission
    of queued work but lets already-running requests finish and be consumed. Once the
    in-flight set is drained, KeyboardInterrupt is raised so callers still exit as an
    interrupted command. A second SIGINT raises KeyboardInterrupt immediately.

    Request threads are daemon threads. On an exception or forced interrupt no new
    requests are started; already-running requests may finish in the background but
    cannot hold process shutdown open.
    """

    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if not items:
        return {}

    results: dict[int, R] = {}
    responses: queue.Queue[tuple[int, U | None, BaseException | None]] = queue.Queue()
    cancelled = threading.Event()
    drain_requested = threading.Event()
    active: dict[int, threading.Thread] = {}
    next_index = 0
    drain_notified = False

    previous_sigint: signal.Handlers | None = None
    handler_installed = False
    can_install_handler = (
        drain_on_interrupt
        and threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGINT")
    )

    def handle_sigint(signum: int, frame: FrameType | None) -> None:
        if drain_requested.is_set():
            raise KeyboardInterrupt
        drain_requested.set()

    if can_install_handler:
        previous_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handle_sigint)
        handler_installed = True

    def start(index: int) -> None:
        item = items[index]

        def run() -> None:
            try:
                value = request(index, item)
            except BaseException as exc:
                responses.put((index, None, exc))
                return
            responses.put((index, value, None))

        thread = threading.Thread(
            target=run,
            name=f"watchlater-llm-request-{index}",
            daemon=True,
        )
        active[index] = thread
        thread.start()

    def note_drain() -> None:
        nonlocal drain_notified
        if not drain_requested.is_set() or drain_notified:
            return
        cancelled.set()
        drain_notified = True
        if on_interrupt is not None:
            on_interrupt(len(active), max(0, len(items) - next_index))

    try:
        initial = min(concurrency, len(items))
        for _ in range(initial):
            if drain_requested.is_set():
                break
            start(next_index)
            next_index += 1

        while active:
            note_drain()
            try:
                if handler_installed:
                    index, value, error = responses.get(timeout=0.1)
                else:
                    index, value, error = responses.get()
            except queue.Empty:
                continue

            active.pop(index, None)
            if error is not None:
                cancelled.set()
                raise error
            assert value is not None
            item = items[index]
            if on_response is not None:
                on_response(index, item, value)
            result = consume(index, item, value)
            results[index] = result

            note_drain()
            if (
                not cancelled.is_set()
                and not drain_requested.is_set()
                and next_index < len(items)
            ):
                start(next_index)
                next_index += 1

        note_drain()
        if drain_requested.is_set():
            raise KeyboardInterrupt
    except BaseException:
        cancelled.set()
        raise
    finally:
        if handler_installed and previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)

    return results
