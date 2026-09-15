from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .llm_prompt import RenderedPrompt


DESCRIPTION_REFINEMENT_GUIDANCE = """This is a second-pass description refinement.

Each supplied item contains the previous cheap-evidence classification and a newly
available video description. Reconsider the previous action/reason using the description
as additional evidence; do not preserve the old answer merely for consistency.

When the description is sufficient, set needs_description=false. Set needs_transcript=true
only when the spoken content is still materially necessary to decide the action; do not
request a transcript just because more evidence could theoretically exist. If a description
was truncated, treat that as a limitation rather than as negative evidence about the video.
"""


@dataclass(frozen=True)
class DescriptionRefinementEvidence:
    video_id: str
    playlist_position: int
    parent_run_id: int
    previous_evidence: dict[str, Any]
    previous_classification: dict[str, Any]
    description: str
    description_source: str
    description_truncated: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "stage": "description_refinement",
            "video_id": self.video_id,
            "playlist_position": self.playlist_position,
            "parent_run_id": self.parent_run_id,
            "previous_evidence": self.previous_evidence,
            "previous_classification": self.previous_classification,
            "description": self.description,
            "description_source": self.description_source,
            "description_truncated": self.description_truncated,
        }


@dataclass(frozen=True)
class DescriptionRefinementTarget:
    parent_run_id: int
    snapshot_id: int
    videos: tuple[DescriptionRefinementEvidence, ...]
    missing_description_video_ids: tuple[str, ...]


def description_refinement_prompt(prompt: RenderedPrompt) -> RenderedPrompt:
    system = prompt.system.rstrip() + "\n\n" + DESCRIPTION_REFINEMENT_GUIDANCE.strip() + "\n"
    digest = hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "base_prompt_sha256": prompt.sha256,
                "stage": "description_refinement",
                "guidance": DESCRIPTION_REFINEMENT_GUIDANCE,
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


def _description_for_video(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    video_id: str,
) -> tuple[str, str] | None:
    source_row = conn.execute(
        """
        SELECT description
        FROM snapshot_entries
        WHERE snapshot_id = ? AND video_id = ?
        """,
        (snapshot_id, video_id),
    ).fetchone()
    if source_row is not None:
        description = source_row["description"]
        if isinstance(description, str) and description.strip():
            return description, "snapshot"

    # Prefer a direct yt-dlp observation when one exists, then fall back to
    # another successful metadata source (e.g. archive recovery).
    row = conn.execute(
        """
        SELECT source, description
        FROM metadata_observations
        WHERE video_id = ?
          AND status = 'found'
          AND description IS NOT NULL
          AND TRIM(description) <> ''
        ORDER BY CASE WHEN source = 'yt-dlp' THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["description"]), str(row["source"])


def description_refinement_evidence(
    conn: sqlite3.Connection,
    parent_run_id: int,
    *,
    limit: int | None = None,
    max_description_chars: int = 4000,
) -> DescriptionRefinementTarget:
    if limit is not None and limit < 0:
        raise ValueError("--limit cannot be negative")
    if max_description_chars < 1:
        raise ValueError("--max-description-chars must be at least 1")

    run = conn.execute(
        """
        SELECT id, snapshot_id, status
        FROM llm_classification_runs
        WHERE id = ?
        """,
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
          AND c.needs_description = 1
        ORDER BY c.playlist_position, c.id
        """,
        (snapshot_id, parent_run_id),
    ).fetchall()

    videos: list[DescriptionRefinementEvidence] = []
    missing: list[str] = []
    for row in rows:
        if row["current_action"] not in (None, "clear"):
            continue
        video_id = str(row["video_id"])
        description_row = _description_for_video(
            conn,
            snapshot_id=snapshot_id,
            video_id=video_id,
        )
        if description_row is None:
            missing.append(video_id)
            continue

        description, source = description_row
        truncated = len(description) > max_description_chars
        if truncated:
            description = description[:max_description_chars]

        videos.append(
            DescriptionRefinementEvidence(
                video_id=video_id,
                playlist_position=int(row["playlist_position"]),
                parent_run_id=parent_run_id,
                previous_evidence=json.loads(row["evidence_json"]),
                previous_classification=json.loads(row["raw_result_json"]),
                description=description,
                description_source=source,
                description_truncated=truncated,
            )
        )
        if limit is not None and len(videos) >= limit:
            break

    return DescriptionRefinementTarget(
        parent_run_id=parent_run_id,
        snapshot_id=snapshot_id,
        videos=tuple(videos),
        missing_description_video_ids=tuple(missing),
    )
