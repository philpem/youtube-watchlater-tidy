from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

from .llm_config import ProviderConfig
from .llm_prompt import ACTIONS, CLASSIFICATION_SCHEMA, RenderedPrompt
from .llm_provider import ChatResponse, chat, parse_json_content
from .progress import ProgressCallback, ProgressEvent
from .reports import latest_snapshot_id

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}
TIMELINESS = {"evergreen", "current", "stale", "unknown"}


@dataclass(frozen=True)
class ClassificationEvidence:
    video_id: str
    playlist_position: int
    original_title: str
    recovered_title: str | None
    recovered_source: str | None
    dearrow_title: str | None
    channel: str | None
    channel_id: str | None
    duration: float | None
    view_count: int | None
    upload_date: str | None
    availability: str | None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClassificationSuggestion:
    video_id: str
    action: str
    topic: str
    content_type: str
    timeliness: str
    quality: float
    confidence: float
    reason: str
    existing_playlist: str | None
    new_queue_proposal: str | None
    destination_confidence: float
    destination_reason: str
    needs_description: bool
    needs_transcript: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "action": self.action,
            "topic": self.topic,
            "content_type": self.content_type,
            "timeliness": self.timeliness,
            "quality": self.quality,
            "confidence": self.confidence,
            "reason": self.reason,
            "destination": {
                "existing_playlist": self.existing_playlist,
                "new_queue_proposal": self.new_queue_proposal,
                "confidence": self.destination_confidence,
                "reason": self.destination_reason,
            },
            "needs_description": self.needs_description,
            "needs_transcript": self.needs_transcript,
        }


@dataclass(frozen=True)
class ClassificationBatchResult:
    suggestions: tuple[ClassificationSuggestion, ...]
    input_sha256: str
    usage: dict[str, Any]
    response_model: str | None


@dataclass(frozen=True)
class ClassificationRunResult:
    suggestions: tuple[ClassificationSuggestion, ...]
    batches: tuple[ClassificationBatchResult, ...]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _dearrow_titles(conn: sqlite3.Connection) -> dict[str, str | None]:
    # DeArrow is an optional independent enrichment layer. A classification
    # branch can operate before or after the DeArrow migration has been merged.
    if not _has_table(conn, "dearrow_lookups"):
        return {}
    rows = conn.execute(
        """
        SELECT d.video_id, d.preferred_title
        FROM dearrow_lookups AS d
        WHERE d.status IN ('found', 'not_found')
          AND NOT EXISTS (
              SELECT 1 FROM dearrow_lookups AS newer
              WHERE newer.video_id = d.video_id
                AND newer.status IN ('found', 'not_found')
                AND newer.id > d.id
          )
        """
    ).fetchall()
    return {str(row["video_id"]): row["preferred_title"] for row in rows}


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def classification_evidence(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    selection_id: int | None = None,
    limit: int | None = None,
    include_decided: bool = False,
) -> list[ClassificationEvidence]:
    """Return cheap video evidence for LLM work.

    Action classification keeps the historical unresolved-only default. Semantic
    annotation may opt into already-decided videos without mutating those decisions.
    """
    if limit is not None and limit < 0:
        raise ValueError("--limit cannot be negative")

    selected_ids: set[str] | None = None
    if selection_id is not None:
        selection = conn.execute(
            "SELECT snapshot_id FROM selections WHERE id = ?",
            (selection_id,),
        ).fetchone()
        if selection is None:
            raise ValueError(f"selection {selection_id} does not exist")
        selected_snapshot = int(selection["snapshot_id"])
        if snapshot_id is not None and snapshot_id != selected_snapshot:
            raise ValueError(
                f"selection {selection_id} belongs to snapshot {selected_snapshot}, not {snapshot_id}"
            )
        snapshot_id = selected_snapshot
        selected_ids = {
            str(row["video_id"])
            for row in conn.execute(
                "SELECT video_id FROM selection_entries WHERE selection_id = ?",
                (selection_id,),
            )
        }
    elif snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    assert snapshot_id is not None
    dearrow = _dearrow_titles(conn)
    rows = conn.execute(
        """
        SELECT e.position, e.video_id, e.title AS original_title,
               e.channel_id AS original_channel_id,
               e.channel AS original_channel,
               e.uploader AS original_uploader,
               e.uploader_id AS original_uploader_id,
               e.duration AS original_duration,
               e.view_count AS original_view_count,
               e.availability AS original_availability,
               m.title AS metadata_title,
               m.source AS metadata_source,
               m.channel_id AS metadata_channel_id,
               m.channel AS metadata_channel,
               m.uploader AS metadata_uploader,
               m.uploader_id AS metadata_uploader_id,
               m.duration AS metadata_duration,
               m.view_count AS metadata_view_count,
               m.upload_date AS metadata_upload_date,
               m.availability AS metadata_availability,
               d.action AS current_action
        FROM snapshot_entries AS e
        LEFT JOIN preferred_metadata AS m ON m.video_id = e.video_id
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = e.snapshot_id AND d.video_id = e.video_id
        WHERE e.snapshot_id = ?
        ORDER BY e.position
        """,
        (snapshot_id,),
    ).fetchall()

    result: list[ClassificationEvidence] = []
    for row in rows:
        video_id = str(row["video_id"])
        if selected_ids is not None and video_id not in selected_ids:
            continue
        # Action classification remains unresolved-only by default. Semantic
        # annotation can include decided videos because annotations are stored
        # separately and never supersede current_decisions.
        if not include_decided and row["current_action"] not in (None, "clear"):
            continue

        original_title = str(row["original_title"] or "")
        metadata_title = row["metadata_title"]
        recovered_title: str | None = None
        recovered_source: str | None = None
        if (
            original_title.casefold() in UNAVAILABLE_TITLES
            and isinstance(metadata_title, str)
            and metadata_title.strip()
        ):
            recovered_title = metadata_title
            recovered_source = row["metadata_source"]

        channel = _first_not_none(
            row["original_channel"],
            row["original_uploader"],
            row["metadata_channel"],
            row["metadata_uploader"],
        )
        channel_id = _first_not_none(
            row["original_channel_id"],
            row["original_uploader_id"],
            row["metadata_channel_id"],
            row["metadata_uploader_id"],
        )
        duration = _first_not_none(row["original_duration"], row["metadata_duration"])
        view_count = _first_not_none(row["original_view_count"], row["metadata_view_count"])

        result.append(
            ClassificationEvidence(
                video_id=video_id,
                playlist_position=int(row["position"]),
                original_title=original_title,
                recovered_title=recovered_title,
                recovered_source=recovered_source,
                dearrow_title=dearrow.get(video_id),
                channel=str(channel) if channel else None,
                channel_id=str(channel_id) if channel_id else None,
                duration=float(duration) if duration is not None else None,
                view_count=int(view_count) if view_count is not None else None,
                upload_date=str(row["metadata_upload_date"])
                if row["metadata_upload_date"]
                else None,
                availability=_first_not_none(
                    row["original_availability"], row["metadata_availability"]
                ),
            )
        )
        if limit is not None and len(result) >= limit:
            break
    return result


def evidence_hash(videos: list[ClassificationEvidence]) -> str:
    payload = [video.as_payload() for video in videos]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def build_messages(
    prompt: RenderedPrompt,
    videos: list[ClassificationEvidence],
) -> list[dict[str, str]]:
    messages = prompt.messages()
    messages.append(
        {
            "role": "user",
            "content": "## Videos to classify\n"
            + json.dumps(
                {"videos": [video.as_payload() for video in videos]},
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
    )
    return messages


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"classification field {field} must be a non-empty string")
    return value


def _nullable_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"classification field {field} must be a number from 0 to 1")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"classification field {field} must be between 0 and 1")
    return result


def validate_response(
    value: dict[str, Any],
    *,
    expected_video_ids: list[str],
    existing_playlists: set[str],
) -> list[ClassificationSuggestion]:
    if set(value) != {"classifications"}:
        raise ValueError("classification response must contain only 'classifications'")
    rows = value.get("classifications")
    if not isinstance(rows, list):
        raise ValueError("classification response 'classifications' must be an array")

    expected = list(expected_video_ids)
    expected_set = set(expected)
    if len(expected_set) != len(expected):
        raise ValueError("expected video IDs contain duplicates")

    suggestions: dict[str, ClassificationSuggestion] = {}
    required = {
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
    }
    destination_required = {
        "existing_playlist",
        "new_queue_proposal",
        "confidence",
        "reason",
    }

    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError(f"classification[{index}] has missing or unexpected fields")
        video_id = _string(row["video_id"], f"classifications[{index}].video_id")
        if video_id not in expected_set:
            raise ValueError(f"classification returned unexpected video_id {video_id!r}")
        if video_id in suggestions:
            raise ValueError(f"classification returned duplicate video_id {video_id!r}")

        action = _string(row["action"], f"{video_id}.action")
        if action not in ACTIONS:
            raise ValueError(f"{video_id}.action must be one of {', '.join(ACTIONS)}")
        timeliness = _string(row["timeliness"], f"{video_id}.timeliness")
        if timeliness not in TIMELINESS:
            raise ValueError(
                f"{video_id}.timeliness must be one of {', '.join(sorted(TIMELINESS))}"
            )

        destination = row["destination"]
        if not isinstance(destination, dict) or set(destination) != destination_required:
            raise ValueError(f"{video_id}.destination has missing or unexpected fields")
        existing = _nullable_string(
            destination["existing_playlist"], f"{video_id}.destination.existing_playlist"
        )
        proposed = _nullable_string(
            destination["new_queue_proposal"], f"{video_id}.destination.new_queue_proposal"
        )
        if existing is not None and existing not in existing_playlists:
            raise ValueError(
                f"{video_id} proposed unknown existing playlist {existing!r}; "
                "use new_queue_proposal instead"
            )
        if proposed is not None and not proposed.startswith("Queue - "):
            raise ValueError(
                f"{video_id}.destination.new_queue_proposal must start with 'Queue - '"
            )
        if existing is not None and proposed is not None:
            raise ValueError(
                f"{video_id}.destination cannot select existing and new playlists together"
            )
        if action == "move" and existing is None and proposed is None:
            raise ValueError(f"{video_id}.action is move but no destination was proposed")

        needs_description = row["needs_description"]
        needs_transcript = row["needs_transcript"]
        if not isinstance(needs_description, bool):
            raise ValueError(f"{video_id}.needs_description must be boolean")
        if not isinstance(needs_transcript, bool):
            raise ValueError(f"{video_id}.needs_transcript must be boolean")

        suggestions[video_id] = ClassificationSuggestion(
            video_id=video_id,
            action=action,
            topic=_string(row["topic"], f"{video_id}.topic"),
            content_type=_string(row["content_type"], f"{video_id}.content_type"),
            timeliness=timeliness,
            quality=_score(row["quality"], f"{video_id}.quality"),
            confidence=_score(row["confidence"], f"{video_id}.confidence"),
            reason=_string(row["reason"], f"{video_id}.reason"),
            existing_playlist=existing,
            new_queue_proposal=proposed,
            destination_confidence=_score(
                destination["confidence"], f"{video_id}.destination.confidence"
            ),
            destination_reason=_string(
                destination["reason"], f"{video_id}.destination.reason", allow_empty=True
            ),
            needs_description=needs_description,
            needs_transcript=needs_transcript,
        )

    missing = [video_id for video_id in expected if video_id not in suggestions]
    if missing:
        raise ValueError("classification response omitted video ID(s): " + ", ".join(missing))
    return [suggestions[video_id] for video_id in expected]


def _batches(videos: list[ClassificationEvidence], batch_size: int) -> list[list[ClassificationEvidence]]:
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    return [videos[index : index + batch_size] for index in range(0, len(videos), batch_size)]


def _classify_batch(
    provider: ProviderConfig,
    prompt: RenderedPrompt,
    videos: list[ClassificationEvidence],
    playlists: set[str],
    progress: ProgressCallback | None,
    phase: str,
) -> ClassificationBatchResult:
    chat_kwargs: dict[str, Any] = {"json_schema": CLASSIFICATION_SCHEMA}
    if progress is not None:
        chat_kwargs.update({"progress": progress, "phase": phase})
    response: ChatResponse = chat(
        provider,
        build_messages(prompt, videos),
        **chat_kwargs,
    )
    value = parse_json_content(response, provider.name)
    suggestions = validate_response(
        value,
        expected_video_ids=[video.video_id for video in videos],
        existing_playlists=playlists,
    )
    return ClassificationBatchResult(
        suggestions=tuple(suggestions),
        input_sha256=evidence_hash(videos),
        usage=response.usage,
        response_model=response.model,
    )


def classify(
    provider: ProviderConfig,
    prompt: RenderedPrompt,
    videos: list[ClassificationEvidence],
    *,
    playlists: set[str],
    batch_size: int = 10,
    progress: ProgressCallback | None = None,
    phase: str = "LLM classification",
) -> ClassificationRunResult:
    batches = _batches(videos, batch_size)
    if not batches:
        return ClassificationRunResult(suggestions=(), batches=())

    if progress is not None:
        progress(
            ProgressEvent(
                phase=phase,
                kind="start",
                completed=0,
                total=len(videos),
                unit="video",
                detail=f"{len(batches)} batch(es)",
            )
        )

    results: dict[int, ClassificationBatchResult] = {}
    completed_videos = 0
    completed_batches = 0
    with ThreadPoolExecutor(max_workers=provider.concurrency) as executor:
        futures = {
            executor.submit(
                _classify_batch,
                provider,
                prompt,
                batch,
                playlists,
                progress,
                phase,
            ): index
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            index = futures[future]
            result = future.result()
            results[index] = result
            completed_videos += len(result.suggestions)
            completed_batches += 1
            if progress is not None:
                progress(
                    ProgressEvent(
                        phase=phase,
                        kind="update",
                        completed=completed_videos,
                        total=len(videos),
                        unit="video",
                        detail=f"batch {completed_batches}/{len(batches)}",
                    )
                )

    ordered_batches = tuple(results[index] for index in range(len(batches)))
    suggestions = tuple(
        suggestion for batch in ordered_batches for suggestion in batch.suggestions
    )
    if progress is not None:
        progress(
            ProgressEvent(
                phase=phase,
                kind="finish",
                completed=len(suggestions),
                total=len(videos),
                unit="video",
                detail=f"{len(batches)} batch(es) complete",
            )
        )
    return ClassificationRunResult(suggestions=suggestions, batches=ordered_batches)
