from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import open_catalogue
from .llm_classification import classify, evidence_hash
from .llm_config import load_project_config
from .llm_prompt import render_prompt
from .progress import ConsoleProgress, add_progress_argument, selected_progress_mode
from .llm_store import (
    cached_run_id,
    classification_cache_key,
    run_payload,
    store_run,
)
from .llm_transcript_refinement import (
    transcript_refinement_evidence,
    transcript_refinement_prompt,
)

DEFAULT_CONFIG = Path("watchlater.toml")
DEFAULT_DB = Path("watchlater.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-llm-transcript",
        description="Refine stored LLM classifications using cached caption transcripts.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run-id", type=int, required=True, help="parent LLM run to refine")
    parser.add_argument("--provider")
    parser.add_argument("--interest-profile")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument(
        "--max-transcript-chars",
        type=int,
        default=12000,
        help="maximum transcript characters sent per video (default: 12000)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore an exact cached refinement and append a fresh child run",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="call the provider without reading/writing the LLM run cache",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show transcript refinement evidence without calling the provider",
    )
    add_progress_argument(parser, include_no_progress=True)
    return parser


def _ephemeral_payload(provider, prompt, videos, result) -> dict:
    return {
        "cache": "disabled",
        "stage": "transcript_refinement",
        "provider": provider.name,
        "configured_model": provider.model,
        "prompt_sha256": prompt.sha256,
        "input_sha256": evidence_hash(videos),
        "video_count": len(result.suggestions),
        "classifications": [item.as_payload() for item in result.suggestions],
        "batches": [
            {
                "input_sha256": batch.input_sha256,
                "response_model": batch.response_model,
                "usage": batch.usage,
                "video_ids": [item.video_id for item in batch.suggestions],
            }
            for batch in result.batches
        ],
    }


def _run(args: argparse.Namespace) -> int:
    if args.refresh and args.no_store:
        raise ValueError("--refresh is meaningless with --no-store")

    config = load_project_config(args.config)
    provider = config.provider(args.provider)
    base_prompt = render_prompt(
        config,
        interest_profile=args.interest_profile,
        prompt_file=args.prompt_file,
    )
    prompt = transcript_refinement_prompt(base_prompt)

    with open_catalogue(args.db) as conn:
        target = transcript_refinement_evidence(
            conn,
            args.run_id,
            limit=args.limit,
            max_transcript_chars=args.max_transcript_chars,
        )
        videos = list(target.videos)
        provider_sha, input_sha, cache_key = (
            classification_cache_key(provider, prompt, videos)
            if videos
            else (None, None, None)
        )

        if videos and not args.dry_run and not args.no_store and not args.refresh:
            cached = cached_run_id(
                conn,
                snapshot_id=target.snapshot_id,
                provider_sha256=provider_sha,
                prompt_sha256=prompt.sha256,
                input_sha256=input_sha,
            )
            if cached is not None:
                output = run_payload(conn, cached)
                output["cache"] = "hit"
                output["missing_transcript_video_ids"] = list(
                    target.missing_transcript_video_ids
                )
                print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
                return 0

    if not videos:
        if target.missing_transcript_video_ids:
            print(
                "No cached transcript is available for the requested refinement. "
                f"Fetch captions with `watchlater-transcript fetch --llm-needs-transcript --run-id {args.run_id}` "
                "or retry transcript acquisition with different language/automatic-caption settings."
            )
        else:
            print("No unresolved needs_transcript videos remain in that LLM run.")
        return 0

    if target.missing_transcript_video_ids:
        print(
            f"warning: {len(target.missing_transcript_video_ids)} video(s) still have no cached "
            "transcript and will not be refined",
            file=sys.stderr,
        )

    if args.dry_run:
        output = {
            "cache": "not-queried",
            "cache_key": cache_key,
            "stage": "transcript_refinement",
            "parent_run_id": target.parent_run_id,
            "provider": provider.name,
            "model": provider.model,
            "provider_sha256": provider_sha,
            "prompt_sha256": prompt.sha256,
            "input_sha256": input_sha,
            "video_count": len(videos),
            "missing_transcript_video_ids": list(target.missing_transcript_video_ids),
            "videos": [video.as_payload() for video in videos],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    with ConsoleProgress(selected_progress_mode(args)) as progress:
        result = classify(
            provider,
            prompt,
            videos,
            playlists=set(config.playlists),
            batch_size=args.batch_size,
            progress=progress,
            phase="LLM transcript refinement",
        )

    if args.no_store:
        output = _ephemeral_payload(provider, prompt, videos, result)
        output["parent_run_id"] = target.parent_run_id
        output["missing_transcript_video_ids"] = list(target.missing_transcript_video_ids)
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    context = {
        "stage": "transcript_refinement",
        "parent_run_id": target.parent_run_id,
        "max_transcript_chars": args.max_transcript_chars,
    }
    with open_catalogue(args.db) as conn:
        run_id = store_run(
            conn,
            snapshot_id=target.snapshot_id,
            provider=provider,
            prompt=prompt,
            videos=videos,
            result=result,
            context=context,
        )
        output = run_payload(conn, run_id)
    output["cache"] = "refresh" if args.refresh else "miss"
    output["missing_transcript_video_ids"] = list(target.missing_transcript_video_ids)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("watchlater-llm-transcript: interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-llm-transcript: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
