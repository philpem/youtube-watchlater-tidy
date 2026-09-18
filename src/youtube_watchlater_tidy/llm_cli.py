from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import open_catalogue
from .llm_annotation import (
    annotate,
    discover_taxonomy,
    even_sample,
    render_annotation_prompt,
)
from .llm_annotation_store import (
    annotation_cache_key,
    annotation_run_payload,
    cached_annotation_run_id,
    latest_annotation_run_id,
    store_annotation_run,
)
from .llm_classification import (
    classification_evidence,
    classify,
    evidence_hash,
)
from .llm_config import load_project_config
from .llm_prompt import CLASSIFICATION_SCHEMA, render_prompt, render_prompt_text
from .llm_provider import chat, parse_json_content
from .llm_refinement import (
    description_refinement_evidence,
    description_refinement_prompt,
)
from .llm_store import (
    cached_run_id,
    classification_cache_key,
    latest_run_id,
    resolve_snapshot_id,
    run_payload,
    store_run,
)

DEFAULT_CONFIG = Path("watchlater.toml")
DEFAULT_DB = Path("watchlater.sqlite3")

PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-llm",
        description="Configure and run OpenAI-compatible Watch Later LLM triage.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"project TOML config (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite catalogue path (default: {DEFAULT_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    providers = sub.add_parser("providers", help="list configured provider profiles")
    providers.set_defaults(command="providers")

    prompt = sub.add_parser("prompt", help="render the effective classification prompt")
    prompt.add_argument("--interest-profile")
    prompt.add_argument("--prompt-file", type=Path)
    prompt.add_argument("--hash-only", action="store_true")
    prompt.add_argument("--no-schema", action="store_true")

    probe = sub.add_parser("probe", help="make a small structured-output test request")
    probe.add_argument("--provider")

    classify_parser = sub.add_parser(
        "classify",
        help="classify unresolved videos, caching validated suggestions as advisory evidence",
    )
    classify_parser.add_argument("--provider")
    classify_parser.add_argument("--interest-profile")
    classify_parser.add_argument("--prompt-file", type=Path)
    classify_parser.add_argument("--snapshot", type=int)
    classify_parser.add_argument("--selection", type=int)
    classify_parser.add_argument("--limit", type=int)
    classify_parser.add_argument("--batch-size", type=int, default=10)
    classify_parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore an exact cached run and append a fresh classification run",
    )
    classify_parser.add_argument(
        "--no-store",
        action="store_true",
        help="call the provider and print validated suggestions without reading/writing the LLM cache",
    )
    classify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the exact cheap evidence and hashes without making LLM requests",
    )

    annotate_parser = sub.add_parser(
        "annotate",
        help="assign stable semantic categories/tags for human review",
    )
    annotate_parser.add_argument("--provider")
    annotate_parser.add_argument("--interest-profile")
    annotate_parser.add_argument("--prompt-file", type=Path)
    annotate_parser.add_argument("--snapshot", type=int)
    annotate_parser.add_argument("--selection", type=int)
    annotate_parser.add_argument("--limit", type=int)
    annotate_parser.add_argument("--batch-size", type=int, default=10)
    annotate_parser.add_argument(
        "--scope",
        choices=("all", "remaining"),
        default="all",
        help="annotate all selected videos or only unresolved videos (default: all)",
    )
    annotate_parser.add_argument(
        "--taxonomy",
        choices=("configured", "discover"),
        default="configured",
        help="use configured review categories or discover a vocabulary from catalogue metadata",
    )
    annotate_parser.add_argument(
        "--taxonomy-sample",
        type=int,
        default=250,
        help="maximum evenly-spaced videos used for taxonomy discovery (default: 250)",
    )
    annotate_parser.add_argument(
        "--max-categories",
        type=int,
        default=20,
        help="maximum discovered categories before Other/Unclear (default: 20)",
    )
    annotate_parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore an exact cached annotation run and append a fresh run",
    )
    annotate_parser.add_argument(
        "--no-store",
        action="store_true",
        help="call the provider and print annotations without using the annotation cache",
    )
    annotate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show annotation evidence/taxonomy inputs without making provider requests",
    )

    annotation_results = sub.add_parser(
        "annotation-results",
        help="show a stored semantic annotation run",
    )
    annotation_results.add_argument("--run-id", type=int)
    annotation_results.add_argument("--snapshot", type=int)

    refine_parser = sub.add_parser(
        "refine-description",
        help="reclassify one stored run's needs_description videos using fetched descriptions",
    )
    refine_parser.add_argument("--run-id", type=int, required=True, help="parent LLM run to refine")
    refine_parser.add_argument("--provider")
    refine_parser.add_argument("--interest-profile")
    refine_parser.add_argument("--prompt-file", type=Path)
    refine_parser.add_argument("--limit", type=int)
    refine_parser.add_argument("--batch-size", type=int, default=5)
    refine_parser.add_argument(
        "--max-description-chars",
        type=int,
        default=4000,
        help="maximum description characters sent per video (default: 4000)",
    )
    refine_parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore an exact cached refinement and append a fresh child run",
    )
    refine_parser.add_argument(
        "--no-store",
        action="store_true",
        help="call the provider without reading/writing the LLM run cache",
    )
    refine_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show refinement evidence without making provider requests",
    )

    results_parser = sub.add_parser(
        "results",
        help="show a stored LLM classification run and current human/rule precedence",
    )
    results_parser.add_argument("--run-id", type=int)
    results_parser.add_argument("--snapshot", type=int)
    return parser


def _cmd_providers(args: argparse.Namespace) -> int:
    config = load_project_config(args.config)
    if not config.providers:
        print("No providers configured.")
        return 0
    for name, provider in config.providers.items():
        default = " *" if name == config.default_provider else ""
        key = provider.api_key_env or "-"
        print(
            f"{name}{default}: preset={provider.preset} model={provider.model} "
            f"base_url={provider.base_url} structured={provider.structured_mode} "
            f"api_key_env={key} concurrency={provider.concurrency}"
        )
    return 0


def _cmd_prompt(args: argparse.Namespace) -> int:
    config = load_project_config(args.config)
    prompt = render_prompt(
        config,
        interest_profile=args.interest_profile,
        prompt_file=args.prompt_file,
    )
    if args.hash_only:
        print(prompt.sha256)
    else:
        print(render_prompt_text(prompt, include_schema=not args.no_schema), end="")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    config = load_project_config(args.config)
    provider = config.provider(args.provider)
    response = chat(
        provider,
        [
            {
                "role": "system",
                "content": "Return only a JSON object matching the requested schema.",
            },
            {
                "role": "user",
                "content": '{"instruction":"Return an object with ok=true."}',
            },
        ],
        json_schema=PROBE_SCHEMA,
    )
    value = parse_json_content(response, provider.name)
    if value.get("ok") is not True:
        raise RuntimeError(
            f"provider {provider.name!r} responded, but probe JSON did not contain ok=true: "
            + json.dumps(value, ensure_ascii=False)
        )
    usage = response.usage
    usage_text = ""
    if usage:
        usage_text = " usage=" + json.dumps(usage, ensure_ascii=False, sort_keys=True)
    print(
        f"Provider {provider.name}: OK model={response.model or provider.model}" + usage_text
    )
    return 0


def _ephemeral_payload(provider, prompt, videos, result) -> dict:
    return {
        "cache": "disabled",
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


def _annotation_ephemeral_payload(provider, prompt, videos, result, taxonomy_source, context):
    return {
        "cache": "disabled",
        "provider": provider.name,
        "configured_model": provider.model,
        "prompt_sha256": prompt.sha256,
        "input_sha256": evidence_hash(videos),
        "taxonomy_source": taxonomy_source,
        "taxonomy": prompt.categories,
        "video_count": len(result.annotations),
        "context": context,
        "annotations": [item.as_payload() for item in result.annotations],
        "batches": [
            {
                "input_sha256": batch.input_sha256,
                "response_model": batch.response_model,
                "usage": batch.usage,
                "video_ids": [item.video_id for item in batch.annotations],
            }
            for batch in result.batches
        ],
    }


def _cmd_annotate(args: argparse.Namespace) -> int:
    if args.refresh and args.no_store:
        raise ValueError("--refresh is meaningless with --no-store")
    if args.taxonomy_sample < 1:
        raise ValueError("--taxonomy-sample must be at least 1")
    if not 3 <= args.max_categories <= 30:
        raise ValueError("--max-categories must be between 3 and 30")

    config = load_project_config(args.config)
    provider = config.provider(args.provider)
    base_prompt = render_prompt(
        config,
        interest_profile=args.interest_profile,
        prompt_file=args.prompt_file,
    )

    with open_catalogue(args.db) as conn:
        snapshot_id = resolve_snapshot_id(conn, args.snapshot, args.selection)
        videos = classification_evidence(
            conn,
            snapshot_id,
            selection_id=args.selection,
            limit=args.limit,
            include_decided=args.scope == "all",
        )

        taxonomy_context = {}
        if args.taxonomy == "configured":
            if not config.review_categories:
                raise ValueError(
                    "no configured review categories; add [llm.review_categories] "
                    "or pass --taxonomy discover"
                )
            categories = config.review_categories
        elif args.dry_run:
            sample = even_sample(videos, args.taxonomy_sample) if videos else []
            output = {
                "stage": "semantic_annotation",
                "dry_run": True,
                "snapshot_id": snapshot_id,
                "selection_id": args.selection,
                "scope": args.scope,
                "taxonomy_source": "discover",
                "taxonomy_pending": True,
                "taxonomy_sample_count": len(sample),
                "taxonomy_sample": [video.as_payload() for video in sample],
                "video_count": len(videos),
                "videos": [video.as_payload() for video in videos],
            }
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        else:
            if not videos:
                print(
                    json.dumps(
                        {
                            "stage": "semantic_annotation",
                            "snapshot_id": snapshot_id,
                            "selection_id": args.selection,
                            "scope": args.scope,
                            "taxonomy_source": "discover",
                            "video_count": 0,
                            "annotations": [],
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            sample = even_sample(videos, args.taxonomy_sample)
            discovery = discover_taxonomy(
                provider,
                sample,
                interest_brief=base_prompt.interest_brief,
                max_categories=args.max_categories,
            )
            categories = discovery.categories
            taxonomy_context["taxonomy_discovery"] = discovery.as_context()
            taxonomy_context["taxonomy_sample_count"] = len(sample)

        prompt = render_annotation_prompt(
            config,
            categories,
            interest_profile=args.interest_profile,
            prompt_file=args.prompt_file,
        )
        provider_sha, input_sha, cache_key = (
            annotation_cache_key(provider, prompt, videos)
            if videos
            else (None, None, None)
        )

        if videos and not args.dry_run and not args.no_store and not args.refresh:
            cached = cached_annotation_run_id(
                conn,
                snapshot_id=snapshot_id,
                provider_sha256=provider_sha,
                prompt_sha256=prompt.sha256,
                input_sha256=input_sha,
            )
            if cached is not None:
                output = annotation_run_payload(conn, cached)
                output["cache"] = "hit"
                print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
                return 0

        if args.dry_run:
            output = {
                "stage": "semantic_annotation",
                "dry_run": True,
                "snapshot_id": snapshot_id,
                "selection_id": args.selection,
                "scope": args.scope,
                "taxonomy_source": args.taxonomy,
                "taxonomy": prompt.categories,
                "provider": provider.name,
                "model": provider.model,
                "provider_sha256": provider_sha,
                "prompt_sha256": prompt.sha256,
                "input_sha256": input_sha,
                "cache_key": cache_key,
                "video_count": len(videos),
                "videos": [video.as_payload() for video in videos],
            }
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

    result = annotate(provider, prompt, videos, batch_size=args.batch_size)
    context = {
        "stage": "semantic_annotation",
        "scope": args.scope,
        **taxonomy_context,
    }

    if args.no_store:
        output = _annotation_ephemeral_payload(
            provider, prompt, videos, result, args.taxonomy, context
        )
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    with open_catalogue(args.db) as conn:
        run_id = store_annotation_run(
            conn,
            snapshot_id=snapshot_id,
            selection_id=args.selection,
            provider=provider,
            prompt=prompt,
            videos=videos,
            result=result,
            taxonomy_source=args.taxonomy,
            context=context,
        )
        output = annotation_run_payload(conn, run_id)
    output["cache"] = "refresh" if args.refresh else "miss"
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_annotation_results(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        run_id = args.run_id
        if run_id is None:
            run_id = latest_annotation_run_id(conn, args.snapshot)
        output = annotation_run_payload(conn, run_id)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    if args.refresh and args.no_store:
        raise ValueError("--refresh is meaningless with --no-store")

    config = load_project_config(args.config)
    provider = config.provider(args.provider)
    prompt = render_prompt(
        config,
        interest_profile=args.interest_profile,
        prompt_file=args.prompt_file,
    )
    with open_catalogue(args.db) as conn:
        snapshot_id = resolve_snapshot_id(conn, args.snapshot, args.selection)
        videos = classification_evidence(
            conn,
            snapshot_id,
            selection_id=args.selection,
            limit=args.limit,
        )
        provider_sha, input_sha, cache_key = classification_cache_key(
            provider, prompt, videos
        ) if videos else (None, None, None)

        if videos and not args.dry_run and not args.no_store and not args.refresh:
            cached = cached_run_id(
                conn,
                snapshot_id=snapshot_id,
                provider_sha256=provider_sha,
                prompt_sha256=prompt.sha256,
                input_sha256=input_sha,
            )
            if cached is not None:
                output = run_payload(conn, cached)
                output["cache"] = "hit"
                print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
                return 0

    if not videos:
        print("No unresolved videos match the classification target.")
        return 0

    if args.dry_run:
        output = {
            "cache": "not-queried",
            "cache_key": cache_key,
            "provider": provider.name,
            "model": provider.model,
            "provider_sha256": provider_sha,
            "prompt_sha256": prompt.sha256,
            "input_sha256": input_sha,
            "video_count": len(videos),
            "videos": [video.as_payload() for video in videos],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    result = classify(
        provider,
        prompt,
        videos,
        playlists=set(config.playlists),
        batch_size=args.batch_size,
    )

    if args.no_store:
        print(
            json.dumps(
                _ephemeral_payload(provider, prompt, videos, result),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    with open_catalogue(args.db) as conn:
        run_id = store_run(
            conn,
            snapshot_id=snapshot_id,
            selection_id=args.selection,
            provider=provider,
            prompt=prompt,
            videos=videos,
            result=result,
        )
        output = run_payload(conn, run_id)
    output["cache"] = "refresh" if args.refresh else "miss"
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_refine_description(args: argparse.Namespace) -> int:
    if args.refresh and args.no_store:
        raise ValueError("--refresh is meaningless with --no-store")

    config = load_project_config(args.config)
    provider = config.provider(args.provider)
    base_prompt = render_prompt(
        config,
        interest_profile=args.interest_profile,
        prompt_file=args.prompt_file,
    )
    prompt = description_refinement_prompt(base_prompt)

    with open_catalogue(args.db) as conn:
        target = description_refinement_evidence(
            conn,
            args.run_id,
            limit=args.limit,
            max_description_chars=args.max_description_chars,
        )
        videos = list(target.videos)
        provider_sha, input_sha, cache_key = classification_cache_key(
            provider, prompt, videos
        ) if videos else (None, None, None)

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
                output["missing_description_video_ids"] = list(
                    target.missing_description_video_ids
                )
                print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
                return 0

    if not videos:
        if target.missing_description_video_ids:
            print(
                "No requested descriptions are available yet. Fetch them with "
                f"`watchlater-metadata enrich --llm-needs-description --run-id {args.run_id}`."
            )
        else:
            print("No unresolved needs_description videos remain in that LLM run.")
        return 0

    if target.missing_description_video_ids:
        print(
            f"warning: {len(target.missing_description_video_ids)} video(s) still have no "
            "description and will not be refined",
            file=sys.stderr,
        )

    if args.dry_run:
        output = {
            "cache": "not-queried",
            "cache_key": cache_key,
            "stage": "description_refinement",
            "parent_run_id": target.parent_run_id,
            "provider": provider.name,
            "model": provider.model,
            "provider_sha256": provider_sha,
            "prompt_sha256": prompt.sha256,
            "input_sha256": input_sha,
            "video_count": len(videos),
            "missing_description_video_ids": list(target.missing_description_video_ids),
            "videos": [video.as_payload() for video in videos],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    result = classify(
        provider,
        prompt,
        videos,
        playlists=set(config.playlists),
        batch_size=args.batch_size,
    )

    if args.no_store:
        output = _ephemeral_payload(provider, prompt, videos, result)
        output["stage"] = "description_refinement"
        output["parent_run_id"] = target.parent_run_id
        output["missing_description_video_ids"] = list(
            target.missing_description_video_ids
        )
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    context = {
        "stage": "description_refinement",
        "parent_run_id": target.parent_run_id,
        "max_description_chars": args.max_description_chars,
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
    output["missing_description_video_ids"] = list(target.missing_description_video_ids)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_results(args: argparse.Namespace) -> int:
    with open_catalogue(args.db) as conn:
        run_id = args.run_id
        if run_id is None:
            run_id = latest_run_id(conn, args.snapshot)
        output = run_payload(conn, run_id)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "providers":
            return _cmd_providers(args)
        if args.command == "prompt":
            return _cmd_prompt(args)
        if args.command == "probe":
            return _cmd_probe(args)
        if args.command == "classify":
            return _cmd_classify(args)
        if args.command == "annotate":
            return _cmd_annotate(args)
        if args.command == "annotation-results":
            return _cmd_annotation_results(args)
        if args.command == "refine-description":
            return _cmd_refine_description(args)
        if args.command == "results":
            return _cmd_results(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-llm: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
