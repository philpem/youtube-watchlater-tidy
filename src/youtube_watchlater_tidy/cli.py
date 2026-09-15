from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from .db import open_catalogue
from .enrichment import candidate_video_ids, enrich_with_ytdlp
from .importer import import_watchlater_json
from .keywords import keyword_rows, render_keywords
from .recovery import (
    DEFAULT_FINDYOUTUBEVIDEO_BASE,
    archive_links,
    latest_archive_lookup,
    recover_with_findyoutubevideo,
)
from .recovery_targets import recovery_candidate_video_ids
from .reports import (
    creator_rows,
    format_duration,
    render_creators,
    render_videos,
    video_rows,
)
from .rules import (
    apply_enabled_rules,
    apply_rule,
    save_rule_from_selection,
    saved_rules,
    set_rule_enabled,
)
from .selection_export import selection_export_text
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

    keywords_parser = subparsers.add_parser(
        "keywords",
        help="show repeated title keywords or adjacent phrases",
    )
    keywords_parser.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    keywords_parser.add_argument("--remaining", action="store_true", help="only unresolved videos")
    keywords_parser.add_argument("--ngram", type=int, choices=(1, 2, 3), default=1)
    keywords_parser.add_argument("--min-count", type=int, default=2)
    keywords_parser.add_argument("--examples", type=int, default=2)
    keywords_parser.add_argument("--limit", type=int, default=50)

    videos_parser = subparsers.add_parser("videos", help="show individual videos")
    videos_parser.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    videos_parser.add_argument("--limit", type=int, default=100, help="maximum rows to show (default: 100)")
    videos_parser.add_argument("--remaining", action="store_true", help="only unresolved videos")
    videos_parser.add_argument(
        "--unknown-creator",
        action="store_true",
        help="only entries with no channel/uploader id or name in the effective metadata",
    )
    videos_parser.add_argument(
        "--unavailable",
        action="store_true",
        help="only source entries originally exported as [Private video] or [Deleted video]",
    )
    recovery_state = videos_parser.add_mutually_exclusive_group()
    recovery_state.add_argument(
        "--recovered",
        action="store_true",
        help="only entries with successful enriched/recovered metadata",
    )
    recovery_state.add_argument(
        "--unrecovered",
        action="store_true",
        help="only entries with no successful enriched/recovered metadata",
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

    recover_parser = subparsers.add_parser(
        "recover",
        help="look for deleted/private videos in public archives",
    )
    recover_target = recover_parser.add_mutually_exclusive_group(required=True)
    recover_target.add_argument(
        "--unavailable",
        action="store_true",
        help="recover unavailable entries from the selected snapshot/range",
    )
    recover_target.add_argument(
        "--video-id",
        action="append",
        dest="video_ids",
        metavar="VIDEO_ID",
        help="recover one exact video id; repeat to target several ids",
    )
    recover_target.add_argument(
        "--selection",
        type=int,
        help="recover unavailable entries contained in a saved selection",
    )
    recover_parser.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    recover_parser.add_argument("--limit", type=int, help="maximum number of videos to look up")
    recover_parser.add_argument(
        "--min-position",
        type=int,
        help="minimum playlist position (only with --unavailable)",
    )
    recover_parser.add_argument(
        "--max-position",
        type=int,
        help="maximum playlist position (only with --unavailable)",
    )
    recover_parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore cached results for the explicitly selected target",
    )
    recover_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show video ids that would be queried without making network requests",
    )
    recover_parser.add_argument(
        "--base-url",
        default=DEFAULT_FINDYOUTUBEVIDEO_BASE,
        help=f"FindYouTubeVideo server (default: {DEFAULT_FINDYOUTUBEVIDEO_BASE})",
    )
    recover_parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="per-video HTTP timeout in seconds (default: 90)",
    )
    recover_parser.add_argument(
        "--no-progress",
        action="store_true",
        help="disable the tqdm progress bar",
    )

    recovery_parser = subparsers.add_parser(
        "recovery",
        help="show the latest cached archive-recovery result for one video",
    )
    recovery_parser.add_argument("video_id")
    recovery_parser.add_argument(
        "--raw",
        action="store_true",
        help="print the complete cached FindYouTubeVideo JSON response",
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

    selection_export = selection_sub.add_parser("export", help="export a selection as JSON or CSV")
    selection_export.add_argument("--snapshot", type=int)
    selection_export.add_argument("--id", type=int, dest="selection_id")
    selection_export.add_argument("--format", choices=("json", "csv"), default="json")
    selection_export.add_argument("--output", type=Path, help="output file (default: stdout)")

    selection_save_rule = selection_sub.add_parser(
        "save-rule",
        help="save the current selection selector as a reusable rule",
    )
    selection_save_rule.add_argument("name")
    selection_save_rule.add_argument("action", choices=ACTIONS)
    selection_save_rule.add_argument("--playlist", help="destination playlist; required for move")
    selection_save_rule.add_argument("--priority", type=int, default=100)
    selection_save_rule.add_argument("--snapshot", type=int)
    selection_save_rule.add_argument("--id", type=int, dest="selection_id")

    rules_parser = subparsers.add_parser("rules", help="manage reusable triage rules")
    rules_sub = rules_parser.add_subparsers(dest="rules_command", required=True)

    rules_list = rules_sub.add_parser("list", help="list saved rules")
    rules_list.add_argument("--enabled-only", action="store_true")

    rules_enable = rules_sub.add_parser("enable", help="enable one saved rule")
    rules_enable.add_argument("rule_id", type=int, metavar="ID")

    rules_disable = rules_sub.add_parser("disable", help="disable one saved rule")
    rules_disable.add_argument("rule_id", type=int, metavar="ID")

    rules_apply = rules_sub.add_parser("apply", help="apply saved rules to unresolved videos")
    rules_apply.add_argument("--id", type=int, dest="rule_id", help="apply only one rule")
    rules_apply.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    rules_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="create preview selections but do not record decisions",
    )

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


def _cmd_keywords(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        rows = keyword_rows(
            conn,
            args.snapshot,
            remaining=args.remaining,
            ngram=args.ngram,
            min_count=args.min_count,
            max_examples=args.examples,
        )
    print(render_keywords(rows, args.limit))
    return 0


def _cmd_videos(args: argparse.Namespace) -> int:
    recovered = True if args.recovered else False if args.unrecovered else None
    with open_catalogue(args.db) as conn:
        rows = video_rows(
            conn,
            args.snapshot,
            remaining=args.remaining,
            unknown_creator=args.unknown_creator,
            unavailable=args.unavailable,
            recovered=recovered,
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


def _render_recovery(row) -> str:
    raw = json.loads(row["raw_json"])
    lines = [
        f"Video ID: {row['video_id']}",
        f"Status: {row['status']}",
        f"Lookup: {row['looked_up_at']} via {row['backend']}",
        f"Verdict: {row['human_verdict'] or '-'}",
        "Saved: "
        f"video={'yes' if row['has_video'] else 'no'} "
        f"metadata={'yes' if row['has_metadata'] else 'no'} "
        f"comments={'yes' if row['has_comments'] else 'no'}",
        f"Finder: {row['source_url'] or '-'}",
    ]
    links = archive_links(raw)
    if links:
        lines.append("Archive links:")
        for link in links:
            paywall = " [possibly paywalled]" if link.maybe_paywalled else ""
            contains = f" ({link.contains})" if link.contains not in ("", "null") else ""
            lines.append(f"  {link.service}: {link.title}{contains}{paywall}")
            lines.append(f"    {link.url}")
            if link.note:
                lines.append(f"    note: {link.note}")
    else:
        lines.append("Archive links: none returned")
    return "\n".join(lines)


def _cmd_recover(args: argparse.Namespace) -> int:
    if (
        args.refresh
        and args.unavailable
        and args.limit is not None
        and args.min_position is None
        and args.max_position is None
    ):
        print(
            "watchlater: warning: --refresh bypasses the cache, so --unavailable "
            "--limit will revisit the first N unavailable entries; use position bounds, "
            "--video-id, or --selection to refresh a particular subset",
            file=sys.stderr,
        )

    with open_catalogue(args.db) as conn:
        video_ids = recovery_candidate_video_ids(
            conn,
            args.snapshot,
            unavailable=args.unavailable,
            video_ids=args.video_ids,
            selection_id=args.selection,
            min_position=args.min_position,
            max_position=args.max_position,
            limit=args.limit,
            refresh=args.refresh,
        )

        if args.dry_run:
            for video_id in video_ids:
                print(video_id)
            print(f"{len(video_ids)} video(s) would be queried")
            return 0

        if not video_ids:
            print("No videos need archive recovery.")
            return 0

        print(
            f"Querying FindYouTubeVideo for {len(video_ids)} video(s)...",
            flush=True,
        )
        result = recover_with_findyoutubevideo(
            conn,
            video_ids,
            base_url=args.base_url,
            timeout=args.timeout,
            show_progress=not args.no_progress,
        )

        single_row = None
        if args.video_ids and len(args.video_ids) == 1:
            single_row = latest_archive_lookup(conn, args.video_ids[0])

    print(
        f"Recovery complete: {result.found} archive hit(s), "
        f"{result.metadata_recovered} with recovered metadata, "
        f"{result.not_found} not found, {result.failed} failed "
        f"({result.attempted} attempted)"
    )
    if single_row is not None:
        print()
        print(_render_recovery(single_row))
    return 0 if result.failed == 0 else 1


def _cmd_recovery(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        row = latest_archive_lookup(conn, args.video_id)
    if row is None:
        raise ValueError(f"no cached archive recovery exists for {args.video_id!r}")
    if args.raw:
        print(json.dumps(json.loads(row["raw_json"]), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render_recovery(row))
    return 0


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


def _render_rules(rows) -> str:
    headers = ["ID", "On", "Priority", "Action", "Name", "Selector"]
    data: list[list[str]] = []
    for rule in rows:
        action = rule.action
        if rule.destination_playlist:
            action = f"{action}->{rule.destination_playlist}"
        data.append(
            [
                str(rule.id),
                "yes" if rule.enabled else "no",
                str(rule.priority),
                action,
                rule.name,
                f"{rule.selector_type}:{json.dumps(rule.selector, ensure_ascii=False, sort_keys=True)}",
            ]
        )
    widths = [len(header) for header in headers]
    for item in data:
        for index, value in enumerate(item):
            widths[index] = max(widths[index], len(value))

    def line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values)).rstrip()

    output = [line(headers), line(["-" * width for width in widths])]
    output.extend(line(item) for item in data)
    output.append(f"{len(data)} rule(s)")
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
        if args.selection_command == "export":
            text = selection_export_text(conn, selection_id, format=args.format)
            if args.output:
                args.output.write_text(text, encoding="utf-8")
                print(f"Selection {selection_id}: wrote {args.format.upper()} to {args.output}")
            else:
                print(text, end="")
            return 0
        if args.selection_command == "save-rule":
            rule_id = save_rule_from_selection(
                conn,
                selection_id,
                name=args.name,
                action=args.action,
                destination_playlist=args.playlist,
                priority=args.priority,
            )
            print(f"Selection {selection_id}: saved rule {rule_id} ({args.name})")
            return 0
    raise RuntimeError("unreachable selection command")


def _cmd_rules(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        if args.rules_command == "list":
            print(_render_rules(saved_rules(conn, enabled_only=args.enabled_only)))
            return 0
        if args.rules_command == "enable":
            set_rule_enabled(conn, args.rule_id, True)
            print(f"Rule {args.rule_id}: enabled")
            return 0
        if args.rules_command == "disable":
            set_rule_enabled(conn, args.rule_id, False)
            print(f"Rule {args.rule_id}: disabled")
            return 0
        if args.rules_command == "apply":
            commit = not args.dry_run
            if args.rule_id is not None:
                results = [apply_rule(conn, args.rule_id, args.snapshot, commit=commit)]
            else:
                results = apply_enabled_rules(conn, args.snapshot, commit=commit)
            for result in results:
                if args.dry_run:
                    print(
                        f"Rule {result.rule_id} ({result.name}): would match {result.matched} video(s) "
                        f"via selection {result.selection_id}"
                    )
                else:
                    print(
                        f"Rule {result.rule_id} ({result.name}): matched {result.matched}, "
                        f"applied {result.applied} via selection {result.selection_id}"
                    )
            if not results:
                print("No enabled rules.")
            return 0
    raise RuntimeError("unreachable rules command")


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
        if args.command == "keywords":
            return _cmd_keywords(args)
        if args.command == "videos":
            return _cmd_videos(args)
        if args.command == "enrich":
            return _cmd_enrich(args)
        if args.command == "recover":
            return _cmd_recover(args)
        if args.command == "recovery":
            return _cmd_recovery(args)
        if args.command == "select":
            return _cmd_select(args)
        if args.command == "selection":
            return _cmd_selection(args)
        if args.command == "rules":
            return _cmd_rules(args)
    except (OSError, ValueError, RuntimeError, sqlite3.IntegrityError) as exc:
        print(f"watchlater: error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
