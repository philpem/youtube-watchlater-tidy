from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .db import open_catalogue
from .enrichment import candidate_video_ids, enrich_with_ytdlp
from .importer import import_watchlater_json
from .reports import (
    creator_rows,
    format_duration,
    render_creators,
    render_videos,
    video_rows,
)
from .triage import (
    ACTIONS,
    apply_selection_action,
    latest_selection_id,
    select_creator,
    select_title,
    selection_rows,
    undo_selection_action,
)

DEFAULT_DB = Path("watchlater.sqlite3")


def _duration(value: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", value, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("use seconds or a value such as 90s, 15m, 1.5h or 2d")
    number = float(match.group(1))
    scale = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    return number * scale


def _add_selection_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    parser.add_argument(
        "--remaining",
        action="store_true",
        help="only select videos without a current decision",
    )
    parser.add_argument("--min-duration", type=_duration)
    parser.add_argument("--max-duration", type=_duration)
    parser.add_argument("--min-position", type=int)
    parser.add_argument("--max-position", type=int)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater",
        description="Triage and organise a large YouTube Watch Later playlist.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite catalogue path (default: {DEFAULT_DB})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import a yt-dlp JSON snapshot")
    import_parser.add_argument("json_file", type=Path)

    snapshots_parser = subparsers.add_parser("snapshots", help="list imported snapshots")
    snapshots_parser.set_defaults(command="snapshots")

    creators_parser = subparsers.add_parser("creators", help="show videos grouped by creator/channel")
    creators_parser.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    creators_parser.add_argument("--limit", type=int, default=50, help="maximum rows to show (default: 50)")
    creators_parser.add_argument("--remaining", action="store_true", help="only unresolved videos")

    videos_parser = subparsers.add_parser("videos", help="show individual videos")
    videos_parser.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    videos_parser.add_argument("--limit", type=int, default=100, help="maximum rows to show (default: 100)")
    videos_parser.add_argument("--remaining", action="store_true", help="only unresolved videos")
    videos_parser.add_argument(
        "--unknown-creator",
        action="store_true",
        help="only entries with no channel/uploader id or name in the effective metadata",
    )

    enrich_parser = subparsers.add_parser(
        "enrich",
        help="fetch richer metadata for selected catalogue entries",
    )
    enrich_target = enrich_parser.add_mutually_exclusive_group(required=True)
    enrich_target.add_argument(
        "--missing-creator",
        action="store_true",
        help="enrich live entries whose flat-playlist metadata has no creator identity",
    )
    enrich_target.add_argument("--video-id", help="enrich one video id from the snapshot")
    enrich_parser.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    enrich_parser.add_argument("--limit", type=int, help="maximum number of videos to fetch")
    enrich_parser.add_argument(
        "--yt-dlp",
        default="yt-dlp",
        help="yt-dlp executable (default: yt-dlp)",
    )
    enrich_parser.add_argument(
        "--refresh",
        action="store_true",
        help="fetch even when a successful yt-dlp observation is already cached",
    )
    enrich_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show video ids that would be enriched without making network requests",
    )
    enrich_parser.add_argument(
        "--no-progress",
        action="store_true",
        help="disable the tqdm progress bar",
    )

    select_parser = subparsers.add_parser("select", help="create and preview a cohort selection")
    select_sub = select_parser.add_subparsers(dest="select_type", required=True)

    select_creator_parser = select_sub.add_parser("creator", help="select one creator/channel cohort")
    select_creator_parser.add_argument("creator", help="channel id, uploader id, or exact creator name")
    _add_selection_filters(select_creator_parser)

    select_title_parser = select_sub.add_parser("title", help="select videos by title")
    title_match = select_title_parser.add_mutually_exclusive_group(required=True)
    title_match.add_argument("--contains")
    title_match.add_argument("--regex")
    select_title_parser.add_argument("--case-sensitive", action="store_true")
    _add_selection_filters(select_title_parser)

    selection_parser = subparsers.add_parser("selection", help="inspect or action the latest selection")
    selection_sub = selection_parser.add_subparsers(dest="selection_command", required=True)

    selection_show = selection_sub.add_parser("show", help="show the latest/current selection")
    selection_show.add_argument("--snapshot", type=int)
    selection_show.add_argument("--id", type=int, dest="selection_id")
    selection_show.add_argument("--limit", type=int, default=100)

    selection_action = selection_sub.add_parser("action", help="apply an action to the current selection")
    selection_action.add_argument("action", choices=ACTIONS)
    selection_action.add_argument("--playlist", help="destination playlist; required for move")
    selection_action.add_argument("--reason")
    selection_action.add_argument("--snapshot", type=int)
    selection_action.add_argument("--id", type=int, dest="selection_id")

    selection_undo = selection_sub.add_parser("undo", help="make the current selection unresolved again")
    selection_undo.add_argument("--reason")
    selection_undo.add_argument("--snapshot", type=int)
    selection_undo.add_argument("--id", type=int, dest="selection_id")

    return parser


def _cmd_import(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        result = import_watchlater_json(conn, args.json_file)
    state = "already imported" if result.already_present else "imported"
    print(
        f"Snapshot {result.snapshot_id}: {state} {result.entry_count} entries "
        f"(sha256 {result.source_sha256[:12]}...)"
    )
    return 0


def _cmd_snapshots(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        rows = conn.execute(
            """
            SELECT id, imported_at, entry_count, playlist_title, source_path, source_sha256
            FROM snapshots ORDER BY id DESC
            """
        ).fetchall()

    if not rows:
        print("No snapshots imported.")
        return 0

    for row in rows:
        print(
            f"{row['id']:>4}  {row['entry_count']:>5} videos  "
            f"{row['imported_at']}  {row['playlist_title'] or '-'}  "
            f"{row['source_sha256'][:12]}...  {row['source_path']}"
        )
    return 0


def _cmd_creators(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        rows = creator_rows(conn, args.snapshot, remaining=args.remaining)
    print(render_creators(rows, args.limit))
    return 0


def _cmd_videos(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        rows = video_rows(
            conn,
            args.snapshot,
            remaining=args.remaining,
            unknown_creator=args.unknown_creator,
        )
    print(render_videos(rows, args.limit))
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        video_ids = candidate_video_ids(
            conn,
            args.snapshot,
            missing_creator=args.missing_creator,
            video_id=args.video_id,
            limit=args.limit,
            refresh=args.refresh,
        )

        if args.dry_run:
            for video_id in video_ids:
                print(video_id)
            print(f"{len(video_ids)} video(s) would be enriched")
            return 0

        if not video_ids:
            print("No videos need enrichment.")
            return 0

        print(f"Enriching {len(video_ids)} video(s) with yt-dlp...", flush=True)
        result = enrich_with_ytdlp(
            conn,
            video_ids,
            yt_dlp=args.yt_dlp,
            show_progress=not args.no_progress,
        )

    print(
        f"Enrichment complete: {result.found} found, {result.failed} failed "
        f"({result.attempted} attempted)"
    )
    return 0 if result.failed == 0 else 1


def _render_selection(rows: list, limit: int | None = None) -> str:
    if limit is not None:
        shown = rows[:limit]
    else:
        shown = rows
    headers = ["Pos", "Duration", "Action", "Creator", "Title", "Video ID"]
    data = []
    for row in shown:
        action = row.current_action or "-"
        if row.destination_playlist:
            action = f"{action}->{row.destination_playlist}"
        data.append(
            [
                str(row.position),
                format_duration(row.duration),
                action,
                row.creator,
                row.title,
                row.video_id,
            ]
        )
    widths = [len(header) for header in headers]
    for item in data:
        for i, value in enumerate(item):
            widths[i] = max(widths[i], len(value))

    def line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    output = [line(headers), line(["-" * width for width in widths])]
    output.extend(line(item) for item in data)
    if limit is not None and len(rows) > limit:
        output.append(f"... {len(rows) - limit} more")
    output.append(f"{len(rows)} video(s) selected")
    return "\n".join(output)


def _cmd_select(args: argparse.Namespace) -> int:
    filters = dict(
        snapshot_id=args.snapshot,
        remaining=args.remaining,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        min_position=args.min_position,
        max_position=args.max_position,
    )
    with open_catalogue(args.db) as conn:
        if args.select_type == "creator":
            result = select_creator(conn, args.creator, **filters)
        else:
            result = select_title(
                conn,
                contains=args.contains,
                regex=args.regex,
                case_sensitive=args.case_sensitive,
                **filters,
            )
        rows = selection_rows(conn, result.selection_id)
    print(f"Selection {result.selection_id} ({result.selector_type}, snapshot {result.snapshot_id})")
    print(_render_selection(rows, 100))
    return 0


def _cmd_selection(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        selection_id = args.selection_id
        if selection_id is None:
            selection_id = latest_selection_id(conn, args.snapshot)
        if args.selection_command == "show":
            rows = selection_rows(conn, selection_id)
            print(f"Selection {selection_id}")
            print(_render_selection(rows, args.limit))
            return 0
        if args.selection_command == "action":
            count = apply_selection_action(
                conn,
                args.action,
                destination_playlist=args.playlist,
                reason=args.reason,
                selection_id=selection_id,
                snapshot_id=args.snapshot,
            )
            suffix = f" -> {args.playlist}" if args.playlist else ""
            print(f"Selection {selection_id}: recorded {args.action}{suffix} for {count} video(s)")
            return 0
        if args.selection_command == "undo":
            count = undo_selection_action(
                conn,
                reason=args.reason,
                selection_id=selection_id,
                snapshot_id=args.snapshot,
            )
            print(f"Selection {selection_id}: cleared current decisions for {count} video(s)")
            return 0
    raise RuntimeError("unreachable selection command")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "import":
            return _cmd_import(args)
        if args.command == "snapshots":
            return _cmd_snapshots(args)
        if args.command == "creators":
            return _cmd_creators(args)
        if args.command == "videos":
            return _cmd_videos(args)
        if args.command == "enrich":
            return _cmd_enrich(args)
        if args.command == "select":
            return _cmd_select(args)
        if args.command == "selection":
            return _cmd_selection(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater: error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
