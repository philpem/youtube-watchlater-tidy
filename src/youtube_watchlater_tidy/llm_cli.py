from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .llm_config import load_project_config
from .llm_prompt import CLASSIFICATION_SCHEMA, render_prompt, render_prompt_text
from .llm_provider import chat, parse_json_content

DEFAULT_CONFIG = Path("watchlater.toml")

PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchlater-llm",
        description="Configure and inspect OpenAI-compatible Watch Later LLM providers.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"project TOML config (default: {DEFAULT_CONFIG})",
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
                "content": 'Return {"ok": true}. This is a connectivity and structured-output probe.',
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
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"watchlater-llm: error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
