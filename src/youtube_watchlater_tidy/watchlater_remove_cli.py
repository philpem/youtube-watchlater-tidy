from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import open_catalogue
from .watchlater_removal import (
    create_removal_plan,
    latest_removal_plan_id,
    removal_plan_payload,
)

DEFAULT_DB = Path("watchlater.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-remove",
        description="Plan selective Watch Later removals from current reviewed decisions.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser(
        "plan",
        help="persist a dry-run removal plan from current archive/delete/move decisions",
    )
    plan.add_argument("--snapshot", type=int)
    plan.add_argument("--output", type=Path)

    show = sub.add_parser("show", help="show the latest or selected removal plan")
    show.add_argument("--run-id", type=int)
    show.add_argument("--snapshot", type=int)
    return parser


def _resolve_run_id(conn, args: argparse.Namespace) -> int:
    return args.run_id if args.run_id is not None else latest_removal_plan_id(conn, args.snapshot)


def _cmd_plan(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        result = create_removal_plan(conn, args.snapshot)
        payload = removal_plan_payload(conn, result.run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.output:
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote Watch Later removal plan {result.run_id} to {args.output}", file=sys.stderr)
    if result.blocked_move_count:
        print(
            f"warning: {result.blocked_move_count} move decision(s) were excluded because "
            "their destination is not confirmed for the same decision event",
            file=sys.stderr,
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        run_id = _resolve_run_id(conn, args)
        payload = removal_plan_payload(conn, run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return _cmd_plan(args)
        if args.command == "show":
            return _cmd_show(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-remove: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
