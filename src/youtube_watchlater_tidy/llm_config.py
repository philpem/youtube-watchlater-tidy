from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


PRESET_BASE_URLS = {
    "ollama": "http://127.0.0.1:11434/v1",
    "vllm": "http://127.0.0.1:8000/v1",
    # Unsloth is a training/model distribution tool rather than a distinct
    # inference wire protocol. Its models are commonly served through vLLM,
    # llama-server, or Ollama; override base_url when using another server.
    "unsloth": "http://127.0.0.1:8000/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "generic": None,
}

PRESET_API_KEY_ENVS = {
    "openrouter": "OPENROUTER_API_KEY",
}

STRUCTURED_MODES = {"json_object", "json_schema", "none"}
DEFAULT_MAX_TOKENS = 6000
RESERVED_HEADER_NAMES = {"authorization", "content-type", "accept", "user-agent"}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    preset: str
    base_url: str
    model: str
    api_key_env: str | None = None
    timeout: float = 120.0
    concurrency: int = 2
    retries: int = 2
    temperature: float = 0.0
    max_tokens: int = DEFAULT_MAX_TOKENS
    stream: bool = False
    structured_mode: str = "json_object"
    extra: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        value = os.environ.get(self.api_key_env)
        if not value:
            raise ValueError(
                f"provider {self.name!r} requires environment variable {self.api_key_env!r}"
            )
        return value


@dataclass(frozen=True)
class InterestProfile:
    name: str
    file: Path | None
    guidance: str | None = None


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    default_provider: str | None
    default_interest_profile: str | None
    providers: dict[str, ProviderConfig]
    interest_profiles: dict[str, InterestProfile]
    playlists: dict[str, str]
    review_categories: dict[str, str] = field(default_factory=dict)

    def provider(self, name: str | None = None) -> ProviderConfig:
        selected = name or self.default_provider
        if not selected:
            raise ValueError("no provider selected; set default_provider or pass --provider")
        try:
            return self.providers[selected]
        except KeyError as exc:
            raise ValueError(f"unknown provider profile {selected!r}") from exc

    def interest_profile(self, name: str | None = None) -> InterestProfile | None:
        selected = name or self.default_interest_profile
        if not selected:
            return None
        try:
            return self.interest_profiles[selected]
        except KeyError as exc:
            raise ValueError(f"unknown interest profile {selected!r}") from exc


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a TOML table")
    return value


def _provider(name: str, raw: dict[str, Any]) -> ProviderConfig:
    forbidden = {"api_key", "token", "secret"}.intersection(raw)
    if forbidden:
        field_name = sorted(forbidden)[0]
        raise ValueError(
            f"provider {name!r}: do not store {field_name!r} in config; use api_key_env"
        )

    preset = str(raw.get("preset", "generic")).casefold()
    if preset not in PRESET_BASE_URLS:
        raise ValueError(
            f"provider {name!r}: unknown preset {preset!r}; expected one of "
            + ", ".join(sorted(PRESET_BASE_URLS))
        )
    base_url = raw.get("base_url", PRESET_BASE_URLS[preset])
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError(f"provider {name!r}: base_url is required for preset {preset!r}")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"provider {name!r}: model is required")

    structured_mode = str(raw.get("structured_mode", "json_object"))
    if structured_mode not in STRUCTURED_MODES:
        raise ValueError(
            f"provider {name!r}: structured_mode must be one of "
            + ", ".join(sorted(STRUCTURED_MODES))
        )

    extra = raw.get("extra", {})
    if not isinstance(extra, dict):
        raise ValueError(f"provider {name!r}: extra must be a TOML table")

    raw_headers = raw.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise ValueError(f"provider {name!r}: headers must be a TOML table")
    headers: dict[str, str] = {}
    for header, value in raw_headers.items():
        header_name = str(header)
        if header_name.casefold() in RESERVED_HEADER_NAMES:
            raise ValueError(
                f"provider {name!r}: header {header_name!r} is reserved and cannot be overridden"
            )
        if not isinstance(value, str):
            raise ValueError(f"provider {name!r}: header {header_name!r} must be a string")
        headers[header_name] = value

    api_key_env = raw.get("api_key_env", PRESET_API_KEY_ENVS.get(preset))
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise ValueError(f"provider {name!r}: api_key_env must be a string")

    config = ProviderConfig(
        name=name,
        preset=preset,
        base_url=base_url.rstrip("/"),
        model=model,
        api_key_env=api_key_env,
        timeout=float(raw.get("timeout", 120.0)),
        concurrency=int(raw.get("concurrency", 2)),
        retries=int(raw.get("retries", 2)),
        temperature=float(raw.get("temperature", 0.0)),
        max_tokens=int(raw.get("max_tokens", DEFAULT_MAX_TOKENS)),
        stream=bool(raw.get("stream", preset == "openrouter")),
        structured_mode=structured_mode,
        extra=dict(extra),
        headers=headers,
    )
    if config.timeout <= 0:
        raise ValueError(f"provider {name!r}: timeout must be positive")
    if config.concurrency < 1:
        raise ValueError(f"provider {name!r}: concurrency must be at least 1")
    if config.retries < 0:
        raise ValueError(f"provider {name!r}: retries cannot be negative")
    if config.max_tokens < 1:
        raise ValueError(f"provider {name!r}: max_tokens must be positive")
    return config


def load_project_config(path: str | Path) -> ProjectConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"LLM config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {config_path}: {exc}") from exc

    provider_rows = _table(data, "providers")
    providers: dict[str, ProviderConfig] = {}
    for name, raw in provider_rows.items():
        if not isinstance(raw, dict):
            raise ValueError(f"providers.{name} must be a TOML table")
        providers[str(name)] = _provider(str(name), raw)

    profile_rows = _table(data, "interest_profiles")
    interest_profiles: dict[str, InterestProfile] = {}
    for name, raw in profile_rows.items():
        if not isinstance(raw, dict):
            raise ValueError(f"interest_profiles.{name} must be a TOML table")
        file_value = raw.get("file")
        prompt_path: Path | None = None
        if file_value is not None:
            if not isinstance(file_value, str):
                raise ValueError(f"interest_profiles.{name}.file must be a string")
            prompt_path = (config_path.parent / file_value).resolve()
        guidance = raw.get("guidance")
        if guidance is not None and not isinstance(guidance, str):
            raise ValueError(f"interest_profiles.{name}.guidance must be a string")
        interest_profiles[str(name)] = InterestProfile(
            name=str(name), file=prompt_path, guidance=guidance
        )

    playlist_rows = _table(data, "playlists")
    playlists: dict[str, str] = {}
    for name, value in playlist_rows.items():
        if isinstance(value, str):
            playlists[str(name)] = value
        elif isinstance(value, dict) and isinstance(value.get("description"), str):
            playlists[str(name)] = str(value["description"])
        else:
            raise ValueError(
                f"playlists.{name} must be a description string or table with description"
            )

    llm_rows = _table(data, "llm")
    review_category_rows = _table(llm_rows, "review_categories")
    review_categories: dict[str, str] = {}
    seen_review_categories: set[str] = set()
    for name, value in review_category_rows.items():
        category_name = str(name).strip()
        if not category_name:
            raise ValueError("llm.review_categories names must be non-empty")
        folded = category_name.casefold()
        if folded in seen_review_categories:
            raise ValueError(f"duplicate llm.review_categories entry {category_name!r}")
        seen_review_categories.add(folded)
        if isinstance(value, str):
            description = value.strip()
        elif isinstance(value, dict) and isinstance(value.get("description"), str):
            description = str(value["description"]).strip()
        else:
            raise ValueError(
                f"llm.review_categories.{name} must be a description string "
                "or table with description"
            )
        if not description:
            raise ValueError(
                f"llm.review_categories.{name} description must be non-empty"
            )
        review_categories[category_name] = description

    default_provider = data.get("default_provider")
    if default_provider is not None and not isinstance(default_provider, str):
        raise ValueError("default_provider must be a string")
    default_interest = data.get("default_interest_profile")
    if default_interest is not None and not isinstance(default_interest, str):
        raise ValueError("default_interest_profile must be a string")

    result = ProjectConfig(
        path=config_path.resolve(),
        default_provider=default_provider,
        default_interest_profile=default_interest,
        providers=providers,
        interest_profiles=interest_profiles,
        playlists=playlists,
        review_categories=review_categories,
    )
    if default_provider is not None:
        result.provider()
    if default_interest is not None:
        result.interest_profile()
    return result
