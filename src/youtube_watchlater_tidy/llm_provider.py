from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .llm_config import ProviderConfig
from .llm_diagnostics import active_diagnostic_log, new_request_id
from .progress import ProgressCallback, ProgressEvent


@dataclass(frozen=True)
class ChatResponse:
    content: str
    usage: dict[str, Any]
    model: str | None
    raw: dict[str, Any]
    finish_reason: str | None = None


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
        "stream": provider.stream,
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


def _message_text_content(
    message: dict[str, Any],
    *,
    provider: ProviderConfig,
    finish_reason: str | None,
    response_model: str | None,
) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        unsupported: list[str] = []
        for index, part in enumerate(content):
            if isinstance(part, str):
                text_parts.append(part)
                continue
            if not isinstance(part, dict):
                unsupported.append(f"{index}:{type(part).__name__}")
                continue
            part_type = part.get("type")
            text = part.get("text")
            if part_type in {"text", "output_text"} and isinstance(text, str):
                text_parts.append(text)
                continue
            unsupported.append(f"{index}:{part_type or 'unknown'}")
        if unsupported:
            raise RuntimeError(
                f"provider {provider.name!r} response contains unsupported content "
                f"part(s): {', '.join(unsupported)}"
            )
        return "".join(text_parts)

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise RuntimeError(
            f"provider {provider.name!r} refused the request: {refusal.strip()[:500]}"
        )

    # Some reasoning/provider implementations return no visible content when the
    # completion exhausts its output budget. Preserve that finish reason so the
    # caller can use its adaptive batch-splitting recovery.
    if content is None and finish_reason == "length":
        return ""

    model_detail = f"; model={response_model!r}" if response_model else ""
    finish_detail = (
        f"; finish_reason={finish_reason!r}" if finish_reason is not None else ""
    )
    keys = ", ".join(sorted(str(key) for key in message))
    raise RuntimeError(
        f"provider {provider.name!r} response has no supported text content"
        f"{finish_detail}{model_detail}; message keys=[{keys}]"
    )


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

    finish_reason = choices[0].get("finish_reason")
    if not isinstance(finish_reason, str):
        finish_reason = None

    usage = raw.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    model = raw.get("model")
    response_model = model if isinstance(model, str) else None
    content = _message_text_content(
        message,
        provider=provider,
        finish_reason=finish_reason,
        response_model=response_model,
    )
    return ChatResponse(
        content=content,
        usage=dict(usage),
        model=response_model,
        raw=raw,
        finish_reason=finish_reason,
    )


def _stream_delta_text(content: Any, provider: ProviderConfig) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for index, part in enumerate(content):
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                raise RuntimeError(
                    f"provider {provider.name!r} stream content part {index} "
                    f"has unsupported type {type(part).__name__}"
                )
            part_type = part.get("type")
            text = part.get("text")
            if part_type in {"text", "output_text"} and isinstance(text, str):
                parts.append(text)
                continue
            raise RuntimeError(
                f"provider {provider.name!r} stream contains unsupported "
                f"content part {index}:{part_type or 'unknown'}"
            )
        return "".join(parts)
    raise RuntimeError(
        f"provider {provider.name!r} stream content has unsupported type "
        f"{type(content).__name__}"
    )


def _parse_streaming_chat_response(
    response: Any,
    provider: ProviderConfig,
    *,
    progress: ProgressCallback | None,
    phase: str,
    started_at: float,
) -> tuple[ChatResponse, str, float | None, int]:
    content_parts: list[str] = []
    raw_lines: list[str] = []
    response_model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    first_activity_seconds: float | None = None
    chars_received = 0
    last_progress_at = 0.0
    chunks = 0

    for raw_line in response:
        line = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, (bytes, bytearray))
            else str(raw_line)
        )
        raw_lines.append(line)
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if not stripped.startswith("data:"):
            continue
        data = stripped[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"provider {provider.name!r} returned invalid streaming JSON: {exc}"
            ) from exc
        if not isinstance(chunk, dict):
            continue

        chunks += 1
        now = time.monotonic()
        if first_activity_seconds is None:
            first_activity_seconds = now - started_at

        model = chunk.get("model")
        if isinstance(model, str):
            response_model = model
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, dict):
            usage = dict(chunk_usage)

        choices = chunk.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                finish_reason = reason
            delta = choice.get("delta")
            if isinstance(delta, dict):
                refusal = delta.get("refusal")
                if isinstance(refusal, str) and refusal.strip():
                    raise RuntimeError(
                        f"provider {provider.name!r} refused the request: "
                        f"{refusal.strip()[:500]}"
                    )
                piece = _stream_delta_text(delta.get("content"), provider)
                if piece:
                    content_parts.append(piece)
                    chars_received += len(piece)

        if progress is not None and (
            last_progress_at == 0.0 or now - last_progress_at >= 1.0
        ):
            progress(
                ProgressEvent(
                    phase=phase,
                    kind="status",
                    detail=(
                        f"{provider.name} responding; "
                        f"{chars_received} chars received"
                    ),
                )
            )
            last_progress_at = now

    content = "".join(content_parts)
    raw = {
        "model": response_model,
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    return (
        ChatResponse(
            content=content,
            usage=usage,
            model=response_model,
            raw=raw,
            finish_reason=finish_reason,
        ),
        "".join(raw_lines),
        first_activity_seconds,
        chunks,
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
    diagnostics = active_diagnostic_log()
    request_id = new_request_id()
    authorization = headers.get("Authorization", "")
    api_key = (
        authorization.removeprefix("Bearer ").strip()
        if authorization.startswith("Bearer ")
        else ""
    )
    diagnostic_secrets = tuple(
        secret for secret in (api_key, authorization) if secret
    )
    request_body = json.loads(payload.decode("utf-8"))

    attempts = provider.retries + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        retry_detail: str | None = None
        if diagnostics is not None:
            diagnostics.write(
                "request",
                {
                    "request_id": request_id,
                    "attempt": attempt + 1,
                    "attempts": attempts,
                    "provider": provider.name,
                    "configured_model": provider.model,
                    "url": url,
                    "headers": headers,
                    "body": request_body,
                },
                secrets=diagnostic_secrets,
            )
        attempt_started = time.monotonic()
        raw_body = ""
        try:
            with opener(request, timeout=provider.timeout) as response:
                headers_received_seconds = time.monotonic() - attempt_started
                status = getattr(response, "status", getattr(response, "code", None))
                response_headers = dict(getattr(response, "headers", {}).items()) if getattr(response, "headers", None) is not None else {}
                generation_id = response_headers.get("X-Generation-Id") or response_headers.get("x-generation-id")
                if progress is not None and provider.stream:
                    progress(
                        ProgressEvent(
                            phase=phase,
                            kind="status",
                            detail=(
                                f"{provider.name} connected in "
                                f"{headers_received_seconds:.1f}s; waiting for stream"
                            ),
                        )
                    )
                try:
                    if provider.stream:
                        parsed, raw_body, first_activity_seconds, stream_chunks = (
                            _parse_streaming_chat_response(
                                response,
                                provider,
                                progress=progress,
                                phase=phase,
                                started_at=attempt_started,
                            )
                        )
                    else:
                        response_payload = response.read()
                        raw_body = response_payload.decode("utf-8", errors="replace")
                        parsed = _parse_chat_response(response_payload, provider)
                        first_activity_seconds = None
                        stream_chunks = 0
                except Exception as exc:
                    if diagnostics is not None:
                        diagnostics.write(
                            "response",
                            {
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "provider": provider.name,
                                "configured_model": provider.model,
                                "status": status,
                                "headers": response_headers,
                                "generation_id": generation_id,
                                "stream": provider.stream,
                                "headers_received_seconds": headers_received_seconds,
                                "total_seconds": time.monotonic() - attempt_started,
                                "raw_body": raw_body,
                                "parse_error": f"{type(exc).__name__}: {exc}",
                            },
                            secrets=diagnostic_secrets,
                        )
                    raise
                total_seconds = time.monotonic() - attempt_started
                if diagnostics is not None:
                    diagnostics.write(
                        "response",
                        {
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "provider": provider.name,
                            "configured_model": provider.model,
                            "response_model": parsed.model,
                            "status": status,
                            "headers": response_headers,
                            "generation_id": generation_id,
                            "stream": provider.stream,
                            "headers_received_seconds": headers_received_seconds,
                            "first_activity_seconds": first_activity_seconds,
                            "total_seconds": total_seconds,
                            "stream_chunks": stream_chunks,
                            "content_chars": len(parsed.content),
                            "finish_reason": parsed.finish_reason,
                            "usage": parsed.usage,
                            "raw_body": raw_body,
                        },
                        secrets=diagnostic_secrets,
                    )
                return parsed
        except HTTPError as exc:
            last_error = exc
            error_body = exc.read() if hasattr(exc, "read") else b""
            if diagnostics is not None:
                diagnostics.write(
                    "error",
                    {
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "provider": provider.name,
                        "configured_model": provider.model,
                        "status": exc.code,
                        "error": f"HTTP {exc.code}: {exc.reason}",
                        "raw_body": error_body.decode("utf-8", errors="replace"),
                    },
                    secrets=diagnostic_secrets,
                )
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt + 1 >= attempts:
                detail = "rate limited" if exc.code == 429 else f"HTTP {exc.code}"
                raise RuntimeError(
                    f"provider {provider.name!r} request failed: {detail}"
                ) from exc
            retry_detail = "rate limited" if exc.code == 429 else f"HTTP {exc.code}"
        except URLError as exc:
            last_error = exc
            if diagnostics is not None:
                diagnostics.write(
                    "error",
                    {
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "provider": provider.name,
                        "configured_model": provider.model,
                        "error": f"URLError: {exc.reason}",
                    },
                    secrets=diagnostic_secrets,
                )
            if attempt + 1 >= attempts:
                raise RuntimeError(
                    f"provider {provider.name!r} request failed: {exc.reason}"
                ) from exc
            retry_detail = f"network error: {exc.reason}"
        except TimeoutError as exc:
            last_error = exc
            if diagnostics is not None:
                diagnostics.write(
                    "error",
                    {
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "provider": provider.name,
                        "configured_model": provider.model,
                        "error": "TimeoutError: request timed out",
                    },
                    secrets=diagnostic_secrets,
                )
            if attempt + 1 >= attempts:
                raise RuntimeError(f"provider {provider.name!r} request timed out") from exc
            retry_detail = "request timed out"

        delay = float(2**attempt)
        if diagnostics is not None:
            diagnostics.write(
                "retry",
                {
                    "request_id": request_id,
                    "attempt": attempt + 1,
                    "provider": provider.name,
                    "configured_model": provider.model,
                    "detail": retry_detail,
                    "delay_seconds": delay,
                },
                secrets=diagnostic_secrets,
            )
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

def merge_usage(*usages: dict[str, Any]) -> dict[str, Any]:
    """Combine OpenAI-compatible usage payloads from multiple attempts."""

    def merge_value(left: Any, right: Any) -> Any:
        if isinstance(left, dict) and isinstance(right, dict):
            merged = dict(left)
            for key, value in right.items():
                merged[key] = merge_value(merged[key], value) if key in merged else value
            return merged
        if (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
        ):
            return left + right
        return right

    result: dict[str, Any] = {}
    for usage in usages:
        for key, value in usage.items():
            result[key] = merge_value(result[key], value) if key in result else value
    return result


def parse_json_content(response: ChatResponse, provider_name: str) -> dict[str, Any]:
    try:
        value = json.loads(response.content)
    except json.JSONDecodeError as exc:
        finish = (
            f"; finish_reason={response.finish_reason!r}"
            if response.finish_reason is not None
            else ""
        )
        raise RuntimeError(
            f"provider {provider_name!r} returned non-JSON message content: {exc}{finish}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"provider {provider_name!r} returned JSON that is not an object")
    return value
