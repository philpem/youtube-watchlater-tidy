from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from youtube_watchlater_tidy.llm_cli import main as llm_main
from youtube_watchlater_tidy.llm_config import ProviderConfig, load_project_config
from youtube_watchlater_tidy.llm_prompt import CLASSIFICATION_SCHEMA, render_prompt
from youtube_watchlater_tidy.llm_provider import ChatResponse, build_chat_request, chat, parse_json_content


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self) -> bytes:
        return self.payload


class LLMFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.interests = self.root / "interests.md"
        self.interests.write_text("I like reverse engineering and retrocomputing.\n", encoding="utf-8")
        self.config_path = self.root / "watchlater.toml"
        self.config_path.write_text(
            """
default_provider = "local"
default_interest_profile = "default"

[providers.local]
preset = "ollama"
model = "qwen3:14b"

[providers.schema]
preset = "vllm"
model = "Qwen/Qwen3-14B"
structured_mode = "json_schema"
api_key_env = "WATCHLATER_TEST_KEY"

[providers.schema.extra]
reasoning_effort = "medium"

[providers.router]
preset = "openrouter"
model = "openai/gpt-5"

[providers.router.headers]
HTTP-Referer = "https://example.invalid/project"
X-Title = "watchlater-test"

[interest_profiles.default]
file = "interests.md"
guidance = "Prefer review when evidence is weak."

[playlists]
"Queue - Retrocomputing" = "Vintage computers and unusual architectures."
""".lstrip(),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        os.environ.pop("WATCHLATER_TEST_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.tmp.cleanup()

    def test_provider_presets_and_relative_interest_file(self) -> None:
        config = load_project_config(self.config_path)
        local = config.provider()
        self.assertEqual(local.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(local.model, "qwen3:14b")
        self.assertEqual(local.max_tokens, 6000)
        profile = config.interest_profile()
        self.assertEqual(profile.file, self.interests.resolve())

    def test_provider_max_tokens_can_override_default(self) -> None:
        custom = self.root / "custom-max-tokens.toml"
        custom.write_text(
            '[providers.local]\npreset="ollama"\nmodel="qwen3:14b"\nmax_tokens=32768\n',
            encoding="utf-8",
        )
        provider = load_project_config(custom).provider("local")
        self.assertEqual(provider.max_tokens, 32768)

    def test_openrouter_preset_key_and_optional_headers(self) -> None:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        config = load_project_config(self.config_path)
        provider = config.provider("router")
        self.assertEqual(provider.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(provider.api_key_env, "OPENROUTER_API_KEY")
        url, headers, _ = build_chat_request(
            provider,
            [{"role": "user", "content": "hello"}],
        )
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-or-test")
        self.assertEqual(headers["HTTP-Referer"], "https://example.invalid/project")
        self.assertEqual(headers["X-Title"], "watchlater-test")

    def test_config_rejects_literal_api_key(self) -> None:
        bad = self.root / "bad.toml"
        bad.write_text(
            '[providers.remote]\npreset="generic"\nbase_url="https://example.invalid/v1"\nmodel="x"\napi_key="secret"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "api_key_env"):
            load_project_config(bad)

    def test_config_rejects_reserved_custom_header(self) -> None:
        bad = self.root / "bad-header.toml"
        bad.write_text(
            '[providers.remote]\npreset="generic"\nbase_url="https://example.invalid/v1"\nmodel="x"\n[providers.remote.headers]\nAuthorization="Bearer secret"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            load_project_config(bad)

    def test_prompt_hash_covers_interest_playlist_and_schema(self) -> None:
        config = load_project_config(self.config_path)
        first = render_prompt(config)
        second = render_prompt(config)
        self.assertEqual(first.sha256, second.sha256)
        self.assertIn("reverse engineering", first.interest_brief)
        self.assertIn("Prefer review", first.interest_brief)
        self.assertIn("Queue - Retrocomputing", first.playlist_guidance)
        self.assertIn("needs_description", json.dumps(CLASSIFICATION_SCHEMA))
        self.assertIn("needs_description", first.messages()[1]["content"])
        self.assertIn("classifications", first.messages()[1]["content"])

        self.interests.write_text("I only want electronics.\n", encoding="utf-8")
        changed = render_prompt(config)
        self.assertNotEqual(first.sha256, changed.sha256)

    def test_request_uses_openai_compatible_endpoint_and_env_key(self) -> None:
        os.environ["WATCHLATER_TEST_KEY"] = "top-secret"
        config = load_project_config(self.config_path)
        provider = config.provider("schema")
        url, headers, payload = build_chat_request(
            provider,
            [{"role": "user", "content": "classify"}],
            json_schema=CLASSIFICATION_SCHEMA,
        )
        body = json.loads(payload)
        self.assertEqual(url, "http://127.0.0.1:8000/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer top-secret")
        self.assertEqual(body["model"], "Qwen/Qwen3-14B")
        self.assertEqual(body["reasoning_effort"], "medium")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertNotIn("top-secret", payload.decode("utf-8"))

    def test_chat_parses_standard_openai_response_and_usage(self) -> None:
        provider = ProviderConfig(
            name="test",
            preset="generic",
            base_url="https://example.invalid/v1",
            model="model-a",
            retries=0,
        )
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _FakeResponse(
                {
                    "model": "model-a",
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                }
            )

        response = chat(
            provider,
            [{"role": "user", "content": "probe"}],
            opener=opener,
        )
        self.assertEqual(seen["url"], "https://example.invalid/v1/chat/completions")
        self.assertEqual(parse_json_content(response, provider.name), {"ok": True})
        self.assertEqual(response.usage["prompt_tokens"], 12)

    def test_chat_accepts_typed_text_content_parts(self) -> None:
        provider = ProviderConfig(
            name="openrouter",
            preset="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="example/model",
            retries=0,
        )

        def opener(request, timeout):
            return _FakeResponse(
                {
                    "model": "example/model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": '{"ok":'},
                                    {"type": "output_text", "text": "true}"},
                                ],
                            },
                        }
                    ],
                }
            )

        response = chat(
            provider,
            [{"role": "user", "content": "probe"}],
            opener=opener,
        )
        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(parse_json_content(response, provider.name), {"ok": True})

    def test_chat_preserves_length_finish_with_null_content(self) -> None:
        provider = ProviderConfig(
            name="openrouter",
            preset="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="reasoning/model",
            retries=0,
        )

        def opener(request, timeout):
            return _FakeResponse(
                {
                    "model": "reasoning/model",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning": "internal reasoning omitted",
                            },
                        }
                    ],
                }
            )

        response = chat(
            provider,
            [{"role": "user", "content": "probe"}],
            opener=opener,
        )
        self.assertEqual(response.content, "")
        self.assertEqual(response.finish_reason, "length")
        self.assertEqual(response.model, "reasoning/model")

    def test_chat_reports_provider_refusal_with_null_content(self) -> None:
        provider = ProviderConfig(
            name="openrouter",
            preset="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="example/model",
            retries=0,
        )

        def opener(request, timeout):
            return _FakeResponse(
                {
                    "model": "example/model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "refusal": "Cannot comply with this request.",
                            },
                        }
                    ],
                }
            )

        with self.assertRaisesRegex(RuntimeError, "refused the request"):
            chat(
                provider,
                [{"role": "user", "content": "probe"}],
                opener=opener,
            )

    def test_chat_rejects_unsupported_non_text_content_parts(self) -> None:
        provider = ProviderConfig(
            name="openrouter",
            preset="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="example/model",
            retries=0,
        )

        def opener(request, timeout):
            return _FakeResponse(
                {
                    "model": "example/model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "image_url", "image_url": {}}],
                            },
                        }
                    ],
                }
            )

        with self.assertRaisesRegex(RuntimeError, "unsupported content part"):
            chat(
                provider,
                [{"role": "user", "content": "probe"}],
                opener=opener,
            )

    def test_cli_keyboard_interrupt_exits_130_without_traceback(self) -> None:
        stderr = io.StringIO()
        with patch(
            "youtube_watchlater_tidy.llm_cli._cmd_annotate",
            side_effect=KeyboardInterrupt,
        ):
            with redirect_stderr(stderr):
                rc = llm_main(["annotate"])
        self.assertEqual(rc, 130)
        self.assertIn("watchlater-llm: interrupted", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_non_json_diagnostic_includes_finish_reason(self) -> None:
        response = ChatResponse(
            content='{"incomplete":',
            usage={},
            model="model-a",
            raw={},
            finish_reason="length",
        )
        with self.assertRaisesRegex(RuntimeError, "finish_reason='length'"):
            parse_json_content(response, "test")

    def test_reserved_fields_cannot_be_overridden_by_extra(self) -> None:
        provider = ProviderConfig(
            name="bad-extra",
            preset="generic",
            base_url="https://example.invalid/v1",
            model="x",
            extra={"model": "override"},
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            build_chat_request(provider, [{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
