from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .db import open_catalogue
from .progress import add_progress_argument, progress_enabled, selected_progress_mode
from .dearrow import (
    DEFAULT_DEARROW_BASE,
    candidate_video_ids,
    enrich_dearrow,
    latest_lookup,
)

DEFAULT_DB = Path("watchlater.sqlite3")


def _duration(value: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", value, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("use seconds or a value such as 4h, 3d, or 2w")
    number = float(match.group(1))
    unit = match.group(2).lower()
    scale = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return number * scale


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-dearrow",
        description="Fetch DeArrow alternate titles into a watchlater catalogue.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    enrich = sub.add_parser("enrich", help="fetch DeArrow title submissions")
    target = enrich.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", dest="all_videos")
    target.add_argument("--video-id", action="append", dest="video_ids", metavar="VIDEO_ID")
    target.add_argument("--selection", type=int, dest="selection_id")
    enrich.add_argument("--snapshot", type=int)
    enrich.add_argument("--remaining", action="store_true")
    enrich.add_argument("--limit", type=int)
    enrich.add_argument(
        "--refresh",
        action="store_true",
        help="ignore cached found/not-found results for the selected target",
    )
    enrich.add_argument(
        "--max-age",
        type=_duration,
        dest="max_age_seconds",
        help="refresh cached found/not-found results older than this age, e.g. 12h, 7d, or 2w",
    )
    enrich.add_argument(
        "--hash-prefix",
        action="store_true",
        help="use DeArrow's SHA-256 prefix lookup so the server does not receive the exact video ID",
    )
    enrich.add_argument("--base-url", default=DEFAULT_DEARROW_BASE)
    enrich.add_argument("--timeout", type=float, default=15.0)
    enrich.add_argument("--workers", type=int, default=4)
    enrich.add_argument("--dry-run", action="store_true")
    add_progress_argument(enrich)
    enrich.add_argument("--no-progress", action="store_true")

    show = sub.add_parser("show", help="show the latest cached DeArrow lookup")
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
            remaining=args.remaining,
            limit=args.limit,
            refresh=args.refresh,
            max_age_seconds=args.max_age_seconds,
        )
        if args.dry_run:
            for video_id in video_ids:
                print(video_id)
            print(f"{len(video_ids)} video(s) would be queried")
            return 0
        if not video_ids:
            print("No videos need DeArrow enrichment.")
            return 0

        print(f"Querying DeArrow for {len(video_ids)} video(s)...", flush=True)
        result = enrich_dearrow(
            conn,
            video_ids,
            base_url=args.base_url,
            timeout=args.timeout,
            hash_prefix=args.hash_prefix,
            workers=args.workers,
            show_progress=progress_enabled(selected_progress_mode(args)),
        )

    print(
        f"DeArrow complete: {result.found} with title submissions, "
        f"{result.preferred} with trusted alternate titles, "
        f"{result.not_found} not found, {result.failed} failed "
        f"({result.attempted} attempted)"
    )
    return 0 if result.failed == 0 else 1


def _cmd_show(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        row = latest_lookup(conn, args.video_id)
    if row is None:
        raise ValueError(f"no cached DeArrow lookup exists for {args.video_id!r}")
    if args.raw:
        print(json.dumps(json.loads(row["raw_json"]), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    titles = json.loads(row["titles_json"])
    print(f"Video ID: {row['video_id']}")
    print(f"Status: {row['status']}")
    print(f"Lookup: {row['looked_up_at']}")
    print(f"Source: {row['source_url'] or '-'}")
    print(f"Preferred DeArrow alternate: {row['preferred_title'] or '-'}")
    print("Submissions:")
    if not titles:
        print("  none")
    for index, title in enumerate(titles, start=1):
        trusted = (
            title.get("original") is not True
            and (
                title.get("locked") is True
                or (isinstance(title.get("votes"), int) and title["votes"] >= 0)
            )
        )
        print(
            f"  {index}. {title['title']} "
            f"votes={title.get('votes')} locked={title.get('locked')} "
            f"original={title.get('original')} trusted_alternate={'yes' if trusted else 'no'} "
            f"UUID={title.get('UUID') or '-'}"
        )
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
        print(f"watchlater-dearrow: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
