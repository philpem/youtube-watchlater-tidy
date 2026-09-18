from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .browser_session import DEFAULT_CDP_ENDPOINT
from .db import open_catalogue
from .multi_destination_support import create_removal_plan
from .watchlater_browser import (
    DEFAULT_BROWSER_PROFILE,
    PlaywrightWatchLaterClient,
    execute_removal_plan,
    open_login_session,
)
from .watchlater_removal import (
    latest_removal_plan_id,
    removal_plan_payload,
)

DEFAULT_DB = Path("watchlater.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-remove",
        description="Plan and safely execute selective Watch Later removals by exact video ID.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="persist a removal plan from current local decisions")
    plan.add_argument("--snapshot", type=int)
    plan.add_argument("--output", type=Path)

    show = sub.add_parser("show", help="show the latest or selected removal plan")
    show.add_argument("--run-id", type=int)
    show.add_argument("--snapshot", type=int)

    login = sub.add_parser(
        "login",
        help="attach to an already-running Chromium browser for manual YouTube login",
    )
    login.add_argument("--cdp-endpoint", default=DEFAULT_CDP_ENDPOINT)

    execute = sub.add_parser("execute", help="inspect or apply a persisted removal plan")
    execute.add_argument("--run-id", type=int)
    execute.add_argument("--snapshot", type=int)
    execute.add_argument("--apply", action="store_true", help="actually click Remove from Watch later")
    execute.add_argument(
        "--confirm-remove",
        action="store_true",
        help="second explicit confirmation required together with --apply",
    )
    execute.add_argument("--max-deletes", type=int, default=10)
    execute.add_argument("--interval", type=float, default=2.0)
    execute.add_argument("--retries", type=int, default=1)
    execute.add_argument("--backoff", type=float, default=2.0)
    execute.add_argument("--user-data-dir", type=Path, default=DEFAULT_BROWSER_PROFILE)
    execute.add_argument(
        "--cdp-endpoint",
        help="attach to a manually launched/authenticated Chromium browser instead of launching Playwright's profile",
    )
    execute.add_argument("--headless", action="store_true")
    execute.add_argument("--channel")
    execute.add_argument("--action-menu-label", default="Action menu")
    execute.add_argument("--remove-label", default="Remove from Watch later")
    execute.add_argument("--max-scrolls", type=int, default=250)
    execute.add_argument("--scroll-pause", type=float, default=0.7)
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
            "not every destination is confirmed for the same decision event",
            file=sys.stderr,
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        run_id = _resolve_run_id(conn, args)
        payload = removal_plan_payload(conn, run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    open_login_session(cdp_endpoint=args.cdp_endpoint)
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    client = None

    def progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    try:
        with open_catalogue(args.db) as conn:
            run_id = _resolve_run_id(conn, args)
            preflight = removal_plan_payload(conn, run_id)
            if args.apply and not args.confirm_remove:
                raise ValueError(
                    "destructive Watch Later removal requires both --apply and --confirm-remove"
                )
            if args.apply and int(preflight["stale_item_count"]):
                raise ValueError(
                    f"Watch Later removal plan {run_id} contains "
                    f"{preflight['stale_item_count']} stale item(s); create a fresh plan"
                )

            if args.apply:
                client = PlaywrightWatchLaterClient(
                    user_data_dir=args.user_data_dir,
                    headless=args.headless,
                    channel=args.channel,
                    cdp_endpoint=args.cdp_endpoint,
                    action_menu_label=args.action_menu_label,
                    remove_label=args.remove_label,
                    max_scrolls=args.max_scrolls,
                    scroll_pause=args.scroll_pause,
                    progress=progress,
                )
            result = execute_removal_plan(
                conn,
                run_id,
                client=client,
                apply=args.apply,
                confirmed=args.confirm_remove,
                max_deletes=args.max_deletes,
                interval=args.interval,
                retries=args.retries,
                backoff=args.backoff,
                progress=progress,
            )
            payload = removal_plan_payload(conn, run_id)
        print(
            json.dumps(
                {
                    "execution": {
                        "run_id": result.run_id,
                        "applied": result.applied,
                        "removed": result.removed,
                        "already_absent": result.already_absent,
                        "not_found": result.not_found,
                        "failed": result.failed,
                        "stale": result.stale,
                        "remaining": result.remaining,
                        "destructive_actions": result.destructive_actions,
                        "run_status": result.run_status,
                    },
                    "plan": payload,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.failed == 0 and result.stale == 0 else 1
    finally:
        if client is not None:
            client.close()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return _cmd_plan(args)
        if args.command == "show":
            return _cmd_show(args)
        if args.command == "login":
            return _cmd_login(args)
        if args.command == "execute":
            return _cmd_execute(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-remove: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
