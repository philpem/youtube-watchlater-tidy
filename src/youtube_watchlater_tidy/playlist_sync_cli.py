from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import open_catalogue
from .playlist_browser import execute_browser_plan
from .playlist_sync import (
    DEFAULT_API_QUOTA_LIMIT,
    DEFAULT_PLAYLIST_CREATE_COST,
    DEFAULT_PLAYLIST_INSERT_COST,
    create_plan,
    import_inventory,
    inventory_payload,
    latest_plan_id,
    load_inventory_file,
    plan_payload,
)
from .youtube_api import (
    DEFAULT_CLIENT_SECRETS,
    DEFAULT_TOKEN_FILE,
    authenticated_client,
    execute_api_plan,
    refresh_inventory,
)

DEFAULT_DB = Path("watchlater.sqlite3")


def _add_oauth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--client-secrets",
        type=Path,
        default=DEFAULT_CLIENT_SECRETS,
        help=f"Google OAuth desktop-client JSON (default: {DEFAULT_CLIENT_SECRETS})",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"OAuth token cache (default: {DEFAULT_TOKEN_FILE})",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-playlist",
        description="Plan and execute idempotent YouTube destination-playlist synchronization.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory", help="manage the local destination-playlist inventory")
    inventory_sub = inventory.add_subparsers(dest="inventory_command", required=True)
    inventory_import = inventory_sub.add_parser(
        "import", help="replace the local inventory from versioned JSON"
    )
    inventory_import.add_argument("file", type=Path)
    inventory_sub.add_parser("show", help="show the currently imported inventory as JSON")
    inventory_refresh = inventory_sub.add_parser(
        "refresh",
        help="refresh owned playlists/membership from the authenticated YouTube Data API",
    )
    _add_oauth_args(inventory_refresh)
    inventory_refresh.add_argument("--no-progress", action="store_true")

    plan = sub.add_parser(
        "plan",
        help="build and persist a dry-run plan from current local move decisions",
    )
    plan.add_argument("--snapshot", type=int)
    plan.add_argument("--backend", choices=("api", "browser"), default="api")
    plan.add_argument(
        "--new-playlist-privacy",
        choices=("private", "unlisted", "public"),
        default="private",
    )
    plan.add_argument("--quota-limit", type=int, default=DEFAULT_API_QUOTA_LIMIT)
    plan.add_argument("--playlist-create-cost", type=int, default=DEFAULT_PLAYLIST_CREATE_COST)
    plan.add_argument("--playlist-insert-cost", type=int, default=DEFAULT_PLAYLIST_INSERT_COST)
    plan.add_argument("--output", type=Path, help="also write the exact stored plan JSON to this file")
    plan.add_argument(
        "--allow-over-quota",
        action="store_true",
        help="return success even when an API plan's estimate exceeds --quota-limit",
    )

    show = sub.add_parser("show", help="show the latest or selected stored playlist sync plan")
    show.add_argument("--run-id", type=int)
    show.add_argument("--snapshot", type=int)

    execute = sub.add_parser(
        "execute",
        help="inspect or apply one persisted playlist plan (dry-run unless --apply)",
    )
    execute.add_argument("--run-id", type=int, help="plan id (default: latest plan)")
    execute.add_argument("--snapshot", type=int, help="snapshot for latest-plan lookup")
    execute.add_argument(
        "--apply",
        action="store_true",
        help="perform external playlist writes; omitted means no network/browser writes",
    )
    execute.add_argument(
        "--allow-over-quota",
        action="store_true",
        help="permit API --apply even when the persisted plan estimate exceeds its quota limit",
    )
    execute.add_argument(
        "--max-writes",
        type=int,
        help="maximum playlist creates + item inserts this invocation; duplicate checks do not count",
    )
    _add_oauth_args(execute)
    return parser


def _cmd_inventory(args: argparse.Namespace) -> int:
    if args.inventory_command == "refresh":
        client = authenticated_client(
            client_secrets=args.client_secrets,
            token_file=args.token,
        )
        with open_catalogue(args.db) as conn:
            result = refresh_inventory(conn, client, show_progress=not args.no_progress)
        print(
            f"Refreshed {result.playlists} playlist(s) / {result.items} item(s) "
            f"from {result.source}; inventory fetched_at={result.fetched_at}"
        )
        return 0

    with open_catalogue(args.db) as conn:
        if args.inventory_command == "import":
            payload = load_inventory_file(args.file)
            result = import_inventory(conn, payload)
            print(
                f"Imported {result.playlists} playlist(s) / {result.items} item(s) "
                f"from {result.source}; inventory fetched_at={result.fetched_at}"
            )
            return 0
        if args.inventory_command == "show":
            print(json.dumps(inventory_payload(conn), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
    raise ValueError(f"unknown inventory command {args.inventory_command!r}")


def _cmd_plan(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        result = create_plan(
            conn,
            args.snapshot,
            backend=args.backend,
            new_playlist_privacy=args.new_playlist_privacy,
            quota_limit=args.quota_limit,
            playlist_create_cost=args.playlist_create_cost,
            playlist_insert_cost=args.playlist_insert_cost,
        )
        payload = plan_payload(conn, result.run_id)

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.output:
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote plan {result.run_id} to {args.output}", file=sys.stderr)

    if result.exceeds_quota and args.backend == "api" and not args.allow_over_quota:
        print(
            f"watchlater-playlist: API plan {result.run_id} estimates {result.estimated_quota} "
            f"quota units, exceeding configured limit {result.quota_limit}; "
            "no YouTube operation was attempted",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        run_id = args.run_id if args.run_id is not None else latest_plan_id(conn, args.snapshot)
        payload = plan_payload(conn, run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        run_id = args.run_id if args.run_id is not None else latest_plan_id(conn, args.snapshot)
        run = conn.execute("SELECT backend FROM playlist_sync_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise ValueError(f"playlist sync plan {run_id} does not exist")
        backend = str(run["backend"])

        if backend == "browser":
            if args.apply:
                raise RuntimeError(
                    "browser playlist UI adapter is not implemented yet; this tranche provides "
                    "the tested executor/checkpoint foundation only"
                )
            result = execute_browser_plan(conn, run_id, apply=False, max_writes=args.max_writes)
        else:
            client = None
            if args.apply:
                client = authenticated_client(
                    client_secrets=args.client_secrets,
                    token_file=args.token,
                )
            result = execute_api_plan(
                conn,
                run_id,
                client=client,
                apply=args.apply,
                allow_over_quota=args.allow_over_quota,
                max_writes=args.max_writes,
            )
        payload = plan_payload(conn, run_id)

    output = {
        "execution": {
            "backend": backend,
            "run_id": result.run_id,
            "applied": result.applied,
            "created_playlists": result.created_playlists,
            "inserted": result.inserted,
            "already_present_this_run": result.already_present,
            "failed_this_run": result.failed,
            "stale_this_run": result.stale,
            "remaining": result.remaining,
            "writes_this_run": result.writes,
            "run_status": result.run_status,
        },
        "plan": payload,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.apply:
        print(
            "Dry run only. API plans can be applied with --apply; browser-plan writes need the "
            "Playwright adapter from the next tranche.",
            file=sys.stderr,
        )
        return 0
    return 0 if result.failed == 0 and result.stale == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            return _cmd_inventory(args)
        if args.command == "plan":
            return _cmd_plan(args)
        if args.command == "show":
            return _cmd_show(args)
        if args.command == "execute":
            return _cmd_execute(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-playlist: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
