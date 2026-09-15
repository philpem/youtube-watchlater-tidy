from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .llm_config import ProjectConfig


ACTIONS = ["keep", "review", "archive", "delete", "move"]

CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classifications"],
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "video_id",
                    "action",
                    "topic",
                    "content_type",
                    "timeliness",
                    "quality",
                    "confidence",
                    "reason",
                    "destination",
                    "needs_description",
                    "needs_transcript",
                ],
                "properties": {
                    "video_id": {"type": "string"},
                    "action": {"type": "string", "enum": ACTIONS},
                    "topic": {"type": "string"},
                    "content_type": {"type": "string"},
                    "timeliness": {
                        "type": "string",
                        "enum": ["evergreen", "current", "stale", "unknown"],
                    },
                    "quality": {"type": "number", "minimum": 0, "maximum": 1},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                    "destination": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "existing_playlist",
                            "new_queue_proposal",
                            "confidence",
                            "reason",
                        ],
                        "properties": {
                            "existing_playlist": {"type": ["string", "null"]},
                            "new_queue_proposal": {"type": ["string", "null"]},
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "reason": {"type": "string"},
                        },
                    },
                    "needs_description": {"type": "boolean"},
                    "needs_transcript": {"type": "boolean"},
                },
            },
        }
    },
}


FIXED_SYSTEM_PROMPT = """You classify videos from a user's YouTube Watch Later catalogue.

This is a late-stage triage step. Human decisions and deterministic saved rules have
higher authority than your suggestions. You are only classifying the videos supplied
to you; do not infer or alter decisions for any other videos.

For each supplied video, propose one action:
- keep: leave it in Watch Later
- review: insufficient evidence or a genuinely ambiguous decision
- archive: worthwhile reference, but it need not stay in Watch Later
- delete: low-value/discard candidate
- move: worthwhile, but better placed in a destination playlist

Use the user's interest brief as preference evidence, not as an absolute exclusion
list. Do not confuse popularity with quality. Do not penalise old material merely for
being old when it is evergreen. Mark transient material stale only when its usefulness
really depends on past timeliness.

Destination rules:
- Prefer an existing playlist from the supplied controlled list when it clearly fits.
- If no existing playlist fits, you may propose a new name beginning with `Queue - `.
- A destination is only a proposal. Never claim that a playlist was created or changed.
- For actions other than move, destination fields should normally be null unless a
  useful future destination suggestion is genuinely relevant.

Evidence escalation:
- Set needs_description when title/channel/available metadata is insufficient and a
  description would probably resolve the uncertainty.
- Set needs_transcript only when the description is unlikely to be enough and the
  spoken content is material to the decision.
- Missing captions or metadata are not negative quality signals.

Return only JSON matching the supplied classification schema. Keep reasons concise
and evidence-based. The video_id in each result must exactly match one supplied ID.
"""


@dataclass(frozen=True)
class RenderedPrompt:
    system: str
    interest_brief: str
    playlist_guidance: str
    profile_name: str | None
    sha256: str

    def messages(self) -> list[dict[str, str]]:
        # The schema is always supplied as text, even when the provider also
        # supports server-side json_schema enforcement. This keeps json_object
        # and compatibility-mode providers semantically equivalent.
        user_sections = [
            "## User interest brief\n" + (self.interest_brief or "No additional user-interest brief supplied."),
            "## Destination playlist guidance\n" + (
                self.playlist_guidance or "No existing destination playlists were supplied."
            ),
            "## Required classification JSON schema\n"
            + json.dumps(CLASSIFICATION_SCHEMA, ensure_ascii=False, sort_keys=True),
        ]
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": "\n\n".join(user_sections)},
        ]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(f"prompt file not found: {path}") from exc


def _playlist_guidance(playlists: dict[str, str]) -> str:
    if not playlists:
        return ""
    return "\n".join(f"- {name}: {description}" for name, description in playlists.items())


def render_prompt(
    config: ProjectConfig,
    *,
    interest_profile: str | None = None,
    prompt_file: str | Path | None = None,
) -> RenderedPrompt:
    profile = config.interest_profile(interest_profile)
    if prompt_file is not None:
        path = Path(prompt_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        interest = _read_text(path)
    elif profile is not None and profile.file is not None:
        interest = _read_text(profile.file)
    else:
        interest = ""

    if profile is not None and profile.guidance:
        if interest:
            interest += "\n\n" + profile.guidance.strip()
        else:
            interest = profile.guidance.strip()

    playlists = _playlist_guidance(config.playlists)
    hash_payload = {
        "prompt_version": 1,
        "system": FIXED_SYSTEM_PROMPT,
        "interest": interest,
        "playlists": config.playlists,
        "schema": CLASSIFICATION_SCHEMA,
    }
    digest = hashlib.sha256(
        json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return RenderedPrompt(
        system=FIXED_SYSTEM_PROMPT,
        interest_brief=interest,
        playlist_guidance=playlists,
        profile_name=profile.name if profile is not None else None,
        sha256=digest,
    )


def render_prompt_text(prompt: RenderedPrompt, *, include_schema: bool = True) -> str:
    lines = [
        f"Prompt SHA256: {prompt.sha256}",
        f"Interest profile: {prompt.profile_name or '-'}",
        "",
        "=== SYSTEM ===",
        prompt.system.rstrip(),
        "",
        "=== USER INTEREST BRIEF ===",
        prompt.interest_brief or "(none)",
        "",
        "=== DESTINATION PLAYLIST GUIDANCE ===",
        prompt.playlist_guidance or "(none)",
    ]
    if include_schema:
        lines.extend(
            [
                "",
                "=== CLASSIFICATION JSON SCHEMA ===",
                json.dumps(CLASSIFICATION_SCHEMA, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
    return "\n".join(lines) + "\n"
