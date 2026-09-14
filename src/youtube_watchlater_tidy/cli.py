from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import connect
from .importer import import_watchlater_json
from .reports import creator_rows, render_creators

DEFAULT_DB = Path("watchlater.sqlite3")


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

    return parser


def _cmd_import(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        result = import_watchlater_json(conn, args.json_file)
    state = "already imported" if result.already_present else "imported"
    print(
        f"Snapshot {result.snapshot_id}: {state} {result.entry_count} entries "
        f"(sha256 {result.source_sha256[:12]}...)"
    )
    return 0


def _cmd_snapshots(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
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
    with connect(args.db) as conn:
        rows = creator_rows(conn, args.snapshot)
    print(render_creators(rows, args.limit))
    return 0


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
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater: error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
