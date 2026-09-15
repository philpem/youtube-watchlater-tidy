from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import open_catalogue
from .transcripts import (
    candidate_video_ids,
    fetch_transcripts,
    latest_transcript,
)

DEFAULT_DB = Path("watchlater.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-transcript",
        description="Fetch and cache YouTube subtitle/caption transcripts without media.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="fetch transcript evidence")
    target = fetch.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", dest="all_videos")
    target.add_argument("--video-id", action="append", dest="video_ids", metavar="VIDEO_ID")
    target.add_argument("--selection", type=int, dest="selection_id")
    target.add_argument(
        "--llm-needs-transcript",
        action="store_true",
        help="target videos marked needs_transcript by a stored LLM run",
    )
    fetch.add_argument("--run-id", type=int, help="LLM run for --llm-needs-transcript (default: latest)")
    fetch.add_argument("--snapshot", type=int)
    fetch.add_argument(
        "--language",
        action="append",
        dest="languages",
        metavar="LANG",
        help="preferred caption language; repeat for ordered fallbacks (default: en)",
    )
    fetch.add_argument(
        "--no-auto",
        action="store_true",
        help="do not fall back to automatically generated captions",
    )
    fetch.add_argument(
        "--include-decided",
        action="store_true",
        help="include videos that already have a current human/rule decision",
    )
    fetch.add_argument("--limit", type=int)
    fetch.add_argument("--refresh", action="store_true")
    fetch.add_argument("--yt-dlp", default="yt-dlp")
    fetch.add_argument("--workers", type=int, default=4)
    fetch.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="minimum seconds between starting yt-dlp metadata requests (default: 0.5)",
    )
    fetch.add_argument("--timeout", type=float, default=30.0)
    fetch.add_argument("--dry-run", action="store_true")
    fetch.add_argument("--no-progress", action="store_true")

    show = sub.add_parser("show", help="show the latest successfully cached transcript")
    show.add_argument("video_id")
    show.add_argument("--raw", action="store_true", help="show stored caption payload and metadata")
    return parser


def _cmd_fetch(args: argparse.Namespace) -> int:
    languages = args.languages or ["en"]
    allow_automatic = not args.no_auto
    with open_catalogue(args.db) as conn:
        video_ids = candidate_video_ids(
            conn,
            args.snapshot,
            all_videos=args.all_videos,
            video_ids=args.video_ids,
            selection_id=args.selection_id,
            llm_needs_transcript=args.llm_needs_transcript,
            run_id=args.run_id,
            remaining=not args.include_decided,
            languages=languages,
            allow_automatic=allow_automatic,
            limit=args.limit,
            refresh=args.refresh,
        )
        if args.dry_run:
            for video_id in video_ids:
                print(video_id)
            print(
                f"{len(video_ids)} video(s) would be queried; "
                f"languages={','.join(languages)} automatic={'yes' if allow_automatic else 'no'}"
            )
            return 0
        if not video_ids:
            print("No videos need transcript acquisition for this target/policy.")
            return 0

        print(f"Fetching captions for {len(video_ids)} video(s)...", flush=True)
        result = fetch_transcripts(
            conn,
            video_ids,
            languages=languages,
            allow_automatic=allow_automatic,
            yt_dlp=args.yt_dlp,
            workers=args.workers,
            start_interval=args.interval,
            timeout=args.timeout,
            show_progress=not args.no_progress,
        )

    print(
        f"Transcript acquisition complete: {result.found} found "
        f"({result.manual} manual, {result.automatic} automatic), "
        f"{result.not_found} without captions, {result.failed} failed "
        f"({result.attempted} attempted)"
    )
    return 0 if result.failed == 0 else 1


def _cmd_show(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        row = latest_transcript(conn, args.video_id)
    if row is None:
        raise ValueError(f"no successful transcript exists for {args.video_id!r}")

    if args.raw:
        print(
            json.dumps(
                {
                    "video_id": row["video_id"],
                    "fetched_at": row["fetched_at"],
                    "source_type": row["source_type"],
                    "language": row["language"],
                    "language_name": row["language_name"],
                    "format": row["format"],
                    "source_url": row["source_url"],
                    "metadata": json.loads(row["metadata_json"]),
                    "segments": json.loads(row["segments_json"]),
                    "raw_text": row["raw_text"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    text = row["transcript_text"] or ""
    print(f"Video ID: {row['video_id']}")
    print(f"Fetched: {row['fetched_at']}")
    print(f"Source: {row['source_type']}")
    print(f"Language: {row['language']} ({row['language_name'] or '-'})")
    print(f"Format: {row['format']}")
    print(f"Length: {len(text)} characters")
    print()
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            return _cmd_fetch(args)
        if args.command == "show":
            return _cmd_show(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-transcript: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
