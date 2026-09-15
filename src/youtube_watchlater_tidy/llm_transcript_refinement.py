from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .llm_prompt import RenderedPrompt
from .transcripts import ensure_transcript_schema


TRANSCRIPT_REFINEMENT_GUIDANCE = """This is a transcript/caption refinement pass.

Each supplied item contains the previous validated classification, its previous evidence,
and a cached caption transcript. Reconsider the previous action and reasoning using the
spoken-content evidence; do not preserve the old answer merely for consistency.

When the transcript evidence is sufficient, set needs_transcript=false. Do not request
more transcript evidence merely because additional context could theoretically exist. If
the supplied transcript was truncated and the omitted content could materially change the
decision, prefer review and leave needs_transcript=true so the user can retry with a larger
transcript budget. Missing captions are handled before this prompt and are not evidence of
low video quality. Manual captions and automatic captions are both usable evidence, but
the source type is supplied so you can account for possible automatic-caption errors.
"""


@dataclass(frozen=True)
class TranscriptRefinementEvidence:
    video_id: str
    playlist_position: int
    parent_run_id: int
    previous_evidence: dict[str, Any]
    previous_classification: dict[str, Any]
    transcript: str
    transcript_source_type: str
    transcript_language: str | None
    transcript_format: str | None
    transcript_fetched_at: str
    transcript_request_key: str
    transcript_truncated: bool
    transcript_original_chars: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "stage": "transcript_refinement",
            "video_id": self.video_id,
            "playlist_position": self.playlist_position,
            "parent_run_id": self.parent_run_id,
            "previous_evidence": self.previous_evidence,
            "previous_classification": self.previous_classification,
            "transcript": self.transcript,
            "transcript_source_type": self.transcript_source_type,
            "transcript_language": self.transcript_language,
            "transcript_format": self.transcript_format,
            "transcript_fetched_at": self.transcript_fetched_at,
            "transcript_request_key": self.transcript_request_key,
            "transcript_truncated": self.transcript_truncated,
            "transcript_original_chars": self.transcript_original_chars,
        }


@dataclass(frozen=True)
class TranscriptRefinementTarget:
    parent_run_id: int
    snapshot_id: int
    videos: tuple[TranscriptRefinementEvidence, ...]
    missing_transcript_video_ids: tuple[str, ...]


def transcript_refinement_prompt(prompt: RenderedPrompt) -> RenderedPrompt:
    system = prompt.system.rstrip() + "\n\n" + TRANSCRIPT_REFINEMENT_GUIDANCE.strip() + "\n"
    digest = hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "base_prompt_sha256": prompt.sha256,
                "stage": "transcript_refinement",
                "guidance": TRANSCRIPT_REFINEMENT_GUIDANCE,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return RenderedPrompt(
        system=system,
        interest_brief=prompt.interest_brief,
        playlist_guidance=prompt.playlist_guidance,
        profile_name=prompt.profile_name,
        sha256=digest,
    )


def transcript_excerpt(text: str, max_chars: int) -> tuple[str, bool]:
    """Return deterministic beginning/middle/end coverage within a character budget."""
    if max_chars < 1:
        raise ValueError("--max-transcript-chars must be at least 1")
    if len(text) <= max_chars:
        return text, False
    if max_chars < 100:
        return text[:max_chars], True

    marker1 = "\n\n[... transcript middle excerpt ...]\n\n"
    marker2 = "\n\n[... transcript final excerpt ...]\n\n"
    available = max_chars - len(marker1) - len(marker2)
    if available < 3:
        return text[:max_chars], True
    first_len = available // 3
    middle_len = available // 3
    last_len = available - first_len - middle_len
    midpoint = len(text) // 2
    middle_start = max(0, midpoint - middle_len // 2)
    middle = text[middle_start : middle_start + middle_len]
    excerpt = (
        text[:first_len]
        + marker1
        + middle
        + marker2
        + text[-last_len:]
    )
    return excerpt[:max_chars], True


def _latest_transcript_row(conn: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    ensure_transcript_schema(conn)
    return conn.execute(
        """
        SELECT *
        FROM transcript_observations
        WHERE video_id = ?
          AND status = 'found'
          AND transcript_text IS NOT NULL
          AND TRIM(transcript_text) <> ''
        ORDER BY id DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()


def transcript_refinement_evidence(
    conn: sqlite3.Connection,
    parent_run_id: int,
    *,
    limit: int | None = None,
    max_transcript_chars: int = 12000,
) -> TranscriptRefinementTarget:
    if limit is not None and limit < 0:
        raise ValueError("--limit cannot be negative")
    if max_transcript_chars < 1:
        raise ValueError("--max-transcript-chars must be at least 1")

    run = conn.execute(
        "SELECT id, snapshot_id, status FROM llm_classification_runs WHERE id = ?",
        (parent_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"LLM classification run {parent_run_id} does not exist")
    if run["status"] != "complete":
        raise ValueError(f"LLM classification run {parent_run_id} is not complete")

    snapshot_id = int(run["snapshot_id"])
    rows = conn.execute(
        """
        SELECT c.video_id, c.playlist_position, c.evidence_json,
               c.raw_result_json, d.action AS current_action
        FROM llm_classifications AS c
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = ? AND d.video_id = c.video_id
        WHERE c.run_id = ?
          AND c.needs_transcript = 1
        ORDER BY c.playlist_position, c.id
        """,
        (snapshot_id, parent_run_id),
    ).fetchall()

    videos: list[TranscriptRefinementEvidence] = []
    missing: list[str] = []
    for row in rows:
        if row["current_action"] not in (None, "clear"):
            continue
        video_id = str(row["video_id"])
        transcript_row = _latest_transcript_row(conn, video_id)
        if transcript_row is None:
            missing.append(video_id)
            continue

        full_text = str(transcript_row["transcript_text"])
        excerpt, truncated = transcript_excerpt(full_text, max_transcript_chars)
        videos.append(
            TranscriptRefinementEvidence(
                video_id=video_id,
                playlist_position=int(row["playlist_position"]),
                parent_run_id=parent_run_id,
                previous_evidence=json.loads(row["evidence_json"]),
                previous_classification=json.loads(row["raw_result_json"]),
                transcript=excerpt,
                transcript_source_type=str(transcript_row["source_type"] or "unknown"),
                transcript_language=(
                    str(transcript_row["language"])
                    if transcript_row["language"] is not None
                    else None
                ),
                transcript_format=(
                    str(transcript_row["format"])
                    if transcript_row["format"] is not None
                    else None
                ),
                transcript_fetched_at=str(transcript_row["fetched_at"]),
                transcript_request_key=str(transcript_row["request_key"]),
                transcript_truncated=truncated,
                transcript_original_chars=len(full_text),
            )
        )
        if limit is not None and len(videos) >= limit:
            break

    return TranscriptRefinementTarget(
        parent_run_id=parent_run_id,
        snapshot_id=snapshot_id,
        videos=tuple(videos),
        missing_transcript_video_ids=tuple(missing),
    )
