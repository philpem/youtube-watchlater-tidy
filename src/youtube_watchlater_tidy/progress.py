from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping, TextIO

from tqdm import tqdm

ProgressMode = Literal["auto", "always", "never"]
ProgressKind = Literal["start", "update", "status", "message", "retry", "finish"]


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    kind: ProgressKind = "update"
    completed: int | None = None
    total: int | None = None
    unit: str = "item"
    detail: str | None = None
    counters: Mapping[str, int] = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], None]


def add_progress_argument(
    parser: argparse.ArgumentParser,
    *,
    include_no_progress: bool = False,
) -> None:
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="progress reporting: auto on a TTY, always, or never (default: auto)",
    )
    if include_no_progress:
        parser.add_argument(
            "--no-progress",
            action="store_true",
            help="compatibility alias for --progress never",
        )


def selected_progress_mode(args: argparse.Namespace) -> ProgressMode:
    if getattr(args, "no_progress", False):
        return "never"
    return getattr(args, "progress", "auto")


def progress_enabled(mode: ProgressMode, stream: TextIO = sys.stderr) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _counter_text(counters: Mapping[str, int]) -> str:
    return " ".join(f"{name}={value}" for name, value in counters.items())


class ConsoleProgress:
    """Render structured progress events without contaminating stdout.

    Interactive terminals get one live tqdm line per phase. Redirected stderr gets
    phase changes, retries and throttled milestones instead of carriage-return spam.
    """

    def __init__(
        self,
        mode: ProgressMode = "auto",
        *,
        stream: TextIO = sys.stderr,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.mode = mode
        self.stream = stream
        self.clock = clock
        self.enabled = mode != "never"
        self.interactive = progress_enabled(mode, stream)
        self._lock = threading.RLock()
        self._bar = None
        self._phase: str | None = None
        self._completed = 0
        self._last_plain_at = 0.0
        self._last_plain_bucket: tuple[str, int] | None = None

    def __enter__(self) -> "ConsoleProgress":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._bar is not None:
                self._bar.close()
                self._bar = None

    def _close_bar(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None

    def _plain(self, event: ProgressEvent, *, force: bool = False) -> None:
        if not self.enabled:
            return

        now = self.clock()
        should_emit = force or event.kind in {"start", "message", "retry", "finish"}
        if event.kind == "status" and event.detail:
            should_emit = force or now - self._last_plain_at >= 10.0

        if event.kind == "update" and event.completed is not None:
            if event.total:
                bucket = min(10, int(event.completed * 10 / max(1, event.total)))
                key = (event.phase, bucket)
                should_emit = (
                    event.completed >= event.total
                    or key != self._last_plain_bucket
                    or now - self._last_plain_at >= 30.0
                )
                if should_emit:
                    self._last_plain_bucket = key
            else:
                should_emit = now - self._last_plain_at >= 30.0

        if not should_emit:
            return

        parts = [f"{event.phase}:"]
        if event.completed is not None:
            progress = str(event.completed)
            if event.total is not None:
                progress += f"/{event.total}"
            parts.append(f"{progress} {event.unit}")
        if event.detail:
            parts.append(event.detail)
        counters = _counter_text(event.counters)
        if counters:
            parts.append(counters)
        print(" ".join(parts), file=self.stream, flush=True)
        self._last_plain_at = now

    def __call__(self, event: ProgressEvent) -> None:
        if not self.enabled:
            return

        with self._lock:
            if not self.interactive:
                self._plain(event)
                return

            if event.kind in {"message", "retry"}:
                message = event.detail or "retrying"
                tqdm.write(f"{event.phase}: {message}", file=self.stream)
                return

            if event.kind == "start":
                self._close_bar()
                self._phase = event.phase
                self._completed = event.completed or 0
                if event.total is None:
                    tqdm.write(
                        f"{event.phase}: {event.detail or 'started'}",
                        file=self.stream,
                    )
                    return
                self._bar = tqdm(
                    total=event.total,
                    initial=self._completed,
                    desc=event.phase,
                    unit=event.unit,
                    dynamic_ncols=True,
                    file=self.stream,
                )
            elif event.phase != self._phase and event.total is not None:
                self._close_bar()
                self._phase = event.phase
                self._completed = 0
                self._bar = tqdm(
                    total=event.total,
                    desc=event.phase,
                    unit=event.unit,
                    dynamic_ncols=True,
                    file=self.stream,
                )

            if self._bar is not None:
                if event.completed is not None:
                    delta = max(0, event.completed - self._completed)
                    if delta:
                        self._bar.update(delta)
                    self._completed = event.completed
                detail_parts = []
                if event.detail:
                    detail_parts.append(event.detail)
                counters = _counter_text(event.counters)
                if counters:
                    detail_parts.append(counters)
                if detail_parts:
                    self._bar.set_postfix_str(" ".join(detail_parts), refresh=True)
                elif event.kind == "status":
                    self._bar.refresh()

                if event.kind == "finish":
                    self._close_bar()
                    self._phase = None
                    self._completed = 0
            elif event.kind in {"status", "finish"} and event.detail:
                tqdm.write(f"{event.phase}: {event.detail}", file=self.stream)


def legacy_message_callback(
    progress: ProgressCallback | None,
    phase: str,
) -> Callable[[str], None] | None:
    if progress is None:
        return None

    def emit(message: str) -> None:
        durable = message.startswith(
            (
                "Failed ",
                "Skipping stale ",
                "Reached --max-",
                "warning:",
                "Warning:",
            )
        )
        progress(
            ProgressEvent(
                phase=phase,
                kind="message" if durable else "status",
                detail=message,
            )
        )

    return emit
