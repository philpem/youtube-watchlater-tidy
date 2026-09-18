from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import open_catalogue
from .progress import add_progress_argument, progress_enabled, selected_progress_mode
from .rich_metadata import candidate_video_ids, enrich_rich_metadata

DEFAULT_DB = Path("watchlater.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-metadata",
        description="Selectively fetch richer public YouTube metadata with yt-dlp.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    enrich = sub.add_parser("enrich", help="fetch full yt-dlp metadata without media")
    target = enrich.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", dest="all_videos")
    target.add_argument("--video-id", action="append", dest="video_ids", metavar="VIDEO_ID")
    target.add_argument("--selection", type=int, dest="selection_id")
    target.add_argument("--missing-description", action="store_true")
    target.add_argument(
        "--llm-needs-description",
        action="store_true",
        help="target videos flagged needs_description by a stored LLM run",
    )
    enrich.add_argument("--run-id", type=int, help="LLM run for --llm-needs-description (default: latest)")
    enrich.add_argument("--snapshot", type=int)
    enrich.add_argument("--remaining", action="store_true", help="skip videos with a current human/rule decision")
    enrich.add_argument("--limit", type=int)
    enrich.add_argument("--refresh", action="store_true")
    enrich.add_argument("--yt-dlp", default="yt-dlp")
    enrich.add_argument("--workers", type=int, default=4)
    enrich.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="minimum seconds between starting yt-dlp requests (default: 0.5)",
    )
    enrich.add_argument("--dry-run", action="store_true")
    add_progress_argument(enrich)
    enrich.add_argument("--no-progress", action="store_true")

    show = sub.add_parser("show", help="show the latest yt-dlp metadata observation")
    show.add_argument("video_id")
    show.add_argument("--raw", action="store_true")
    return parser


def _cmd_enrich(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        video_ids = candidate_video_ids(
            conn,
            args.snapshot,
            all_videos=args.all_videos,
            video_ids=args.video_ids,
            selection_id=args.selection_id,
            missing_description=args.missing_description,
            llm_needs_description=args.llm_needs_description,
            run_id=args.run_id,
            remaining=args.remaining,
            limit=args.limit,
            refresh=args.refresh,
        )
        if args.dry_run:
            for video_id in video_ids:
                print(video_id)
            print(f"{len(video_ids)} video(s) would be enriched")
            return 0
        if not video_ids:
            print("No videos need richer metadata enrichment.")
            return 0

        print(f"Fetching richer yt-dlp metadata for {len(video_ids)} video(s)...", flush=True)
        result = enrich_rich_metadata(
            conn,
            video_ids,
            yt_dlp=args.yt_dlp,
            workers=args.workers,
            start_interval=args.interval,
            show_progress=progress_enabled(selected_progress_mode(args)),
        )

    print(
        f"Metadata enrichment complete: {result.found} found, {result.failed} failed "
        f"({result.attempted} attempted)"
    )
    return 0 if result.failed == 0 else 1


def _cmd_show(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        row = conn.execute(
            """
            SELECT * FROM metadata_observations
            WHERE video_id = ? AND source = 'yt-dlp'
            ORDER BY id DESC LIMIT 1
            """,
            (args.video_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"no yt-dlp metadata observation exists for {args.video_id!r}")

    raw = json.loads(row["raw_json"])
    if args.raw:
        print(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"Video ID: {row['video_id']}")
    print(f"Status: {row['status']}")
    print(f"Observed: {row['observed_at']}")
    print(f"Title: {row['title'] or '-'}")
    print(f"Channel: {row['channel'] or row['uploader'] or '-'}")
    print(f"Upload date: {row['upload_date'] or '-'}")
    print(f"Availability: {row['availability'] or '-'}")
    description = row["description"] or ""
    print(f"Description: {'yes' if description.strip() else 'no'} ({len(description)} chars)")
    if isinstance(raw, dict):
        print(f"Live status: {raw.get('live_status') or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "enrich":
            return _cmd_enrich(args)
        if args.command == "show":
            return _cmd_show(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-metadata: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
