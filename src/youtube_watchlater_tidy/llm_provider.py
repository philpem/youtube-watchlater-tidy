from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .llm_config import ProviderConfig
from .progress import ProgressCallback, ProgressEvent


@dataclass(frozen=True)
class ChatResponse:
    content: str
    usage: dict[str, Any]
    model: str | None
    raw: dict[str, Any]


FORBIDDEN_EXTRA_KEYS = {
    "model",
    "messages",
    "response_format",
    "temperature",
    "max_tokens",
    "stream",
}


def _response_format(
    provider: ProviderConfig,
    json_schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if provider.structured_mode == "none":
        return None
    if provider.structured_mode == "json_object":
        return {"type": "json_object"}
    if json_schema is None:
        raise ValueError(
            f"provider {provider.name!r} uses json_schema mode but no JSON schema was supplied"
        )
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "watchlater_classification",
            "strict": True,
            "schema": json_schema,
        },
    }


def build_chat_request(
    provider: ProviderConfig,
    messages: list[dict[str, str]],
    *,
    json_schema: dict[str, Any] | None = None,
) -> tuple[str, dict[str, str], bytes]:
    clashes = FORBIDDEN_EXTRA_KEYS.intersection(provider.extra)
    if clashes:
        raise ValueError(
            f"provider {provider.name!r}: extra cannot override reserved field(s): "
            + ", ".join(sorted(clashes))
        )

    body: dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "stream": False,
    }
    response_format = _response_format(provider, json_schema)
    if response_format is not None:
        body["response_format"] = response_format
    body.update(provider.extra)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "youtube-watchlater-tidy/0.1 (+https://github.com/philpem/youtube-watchlater-tidy)",
    }
    headers.update(provider.headers)
    api_key = provider.api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = provider.base_url.rstrip("/") + "/chat/completions"
    return url, headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


def _parse_chat_response(payload: bytes, provider: ProviderConfig) -> ChatResponse:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"provider {provider.name!r} returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"provider {provider.name!r} returned non-object JSON")

    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError(f"provider {provider.name!r} response has no choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"provider {provider.name!r} response has no choices[0].message")
    content = message.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"provider {provider.name!r} response content is not text")

    usage = raw.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    model = raw.get("model")
    return ChatResponse(
        content=content,
        usage=dict(usage),
        model=model if isinstance(model, str) else None,
        raw=raw,
    )


def chat(
    provider: ProviderConfig,
    messages: list[dict[str, str]],
    *,
    json_schema: dict[str, Any] | None = None,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    progress: ProgressCallback | None = None,
    phase: str = "LLM provider",
) -> ChatResponse:
    url, headers, payload = build_chat_request(
        provider,
        messages,
        json_schema=json_schema,
    )
    request = Request(url, data=payload, headers=headers, method="POST")

    attempts = provider.retries + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        retry_detail: str | None = None
        try:
            with opener(request, timeout=provider.timeout) as response:
                return _parse_chat_response(response.read(), provider)
        except HTTPError as exc:
            last_error = exc
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt + 1 >= attempts:
                detail = "rate limited" if exc.code == 429 else f"HTTP {exc.code}"
                raise RuntimeError(
                    f"provider {provider.name!r} request failed: {detail}"
                ) from exc
            retry_detail = "rate limited" if exc.code == 429 else f"HTTP {exc.code}"
        except URLError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise RuntimeError(
                    f"provider {provider.name!r} request failed: {exc.reason}"
                ) from exc
            retry_detail = f"network error: {exc.reason}"
        except TimeoutError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise RuntimeError(f"provider {provider.name!r} request timed out") from exc
            retry_detail = "request timed out"

        delay = float(2**attempt)
        if progress is not None:
            progress(
                ProgressEvent(
                    phase=phase,
                    kind="retry",
                    detail=(
                        f"{provider.name}: {retry_detail}; "
                        f"retry {attempt + 2}/{attempts} in {delay:g}s"
                    ),
                )
            )
        sleeper(delay)

    assert last_error is not None
    raise RuntimeError(f"provider {provider.name!r} request failed: {last_error}")

def parse_json_content(response: ChatResponse, provider_name: str) -> dict[str, Any]:
    try:
        value = json.loads(response.content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"provider {provider_name!r} returned non-JSON message content: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"provider {provider_name!r} returned JSON that is not an object")
    return value
