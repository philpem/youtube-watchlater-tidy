from __future__ import annotations

import queue
import threading
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
) -> dict[int, R]:
    """Run bounded concurrent requests and consume responses on the caller thread.

    Only the configured concurrency may be in flight. A replacement request is not
    started until the previous response has been successfully consumed/validated.

    Request threads are daemon threads. On an exception or KeyboardInterrupt no new
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
    active: dict[int, threading.Thread] = {}
    next_index = 0

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

    initial = min(concurrency, len(items))
    for _ in range(initial):
        start(next_index)
        next_index += 1

    try:
        while active:
            index, value, error = responses.get()
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

            if not cancelled.is_set() and next_index < len(items):
                start(next_index)
                next_index += 1
    except BaseException:
        cancelled.set()
        raise

    return results
