from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import open_catalogue
from .review_report import apply_review_decisions, load_review_decisions, write_review_report

DEFAULT_DB = Path("watchlater.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-review",
        description="Build a self-contained human review report and import explicit overrides.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a self-contained local HTML review report")
    build.add_argument("output", type=Path)
    build.add_argument("--snapshot", type=int)

    imp = sub.add_parser("import", help="import explicit human overrides exported by the report")
    imp.add_argument("decisions", type=Path)
    imp.add_argument("--dry-run", action="store_true")
    return parser


def _cmd_build(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        snapshot_id, count = write_review_report(conn, args.output, args.snapshot)
    print(f"Wrote {args.output} with {count} video(s) from snapshot {snapshot_id}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    payload = load_review_decisions(args.decisions)
    with open_catalogue(args.db) as conn:
        result = apply_review_decisions(conn, payload, dry_run=args.dry_run)
    prefix = "Would record" if args.dry_run else "Recorded"
    print(
        f"{prefix} {result.changed} human override(s) for snapshot {result.snapshot_id}; "
        f"{result.unchanged} already identical ({result.requested} requested)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return _cmd_build(args)
        if args.command == "import":
            return _cmd_import(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-review: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
