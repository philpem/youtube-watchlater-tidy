from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .llm_classification import (
    ClassificationEvidence,
    ClassificationRunResult,
    ClassificationSuggestion,
    evidence_hash,
)
from .llm_config import ProviderConfig
from .llm_prompt import RenderedPrompt
from .reports import latest_snapshot_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def provider_fingerprint(provider: ProviderConfig) -> str:
    """Hash output-affecting provider configuration, never secret values."""
    return _canonical_hash(
        {
            "version": 1,
            "name": provider.name,
            "preset": provider.preset,
            "base_url": provider.base_url,
            "model": provider.model,
            "temperature": provider.temperature,
            "max_tokens": provider.max_tokens,
            "structured_mode": provider.structured_mode,
            "extra": provider.extra,
        }
    )


def evidence_item_hash(video: ClassificationEvidence) -> str:
    return _canonical_hash(video.as_payload())


def classification_cache_key(
    provider: ProviderConfig,
    prompt: RenderedPrompt,
    videos: list[ClassificationEvidence],
) -> tuple[str, str, str]:
    provider_sha = provider_fingerprint(provider)
    input_sha = evidence_hash(videos)
    key = _canonical_hash(
        {
            "version": 1,
            "provider_sha256": provider_sha,
            "prompt_sha256": prompt.sha256,
            "input_sha256": input_sha,
        }
    )
    return provider_sha, input_sha, key


def resolve_snapshot_id(
    conn: sqlite3.Connection,
    snapshot_id: int | None,
    selection_id: int | None,
) -> int:
    if selection_id is not None:
        row = conn.execute(
            "SELECT snapshot_id FROM selections WHERE id = ?", (selection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"selection {selection_id} does not exist")
        selected = int(row["snapshot_id"])
        if snapshot_id is not None and snapshot_id != selected:
            raise ValueError(
                f"selection {selection_id} belongs to snapshot {selected}, not {snapshot_id}"
            )
        return selected
    return latest_snapshot_id(conn) if snapshot_id is None else snapshot_id


def cached_run_id(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    provider_sha256: str,
    prompt_sha256: str,
    input_sha256: str,
) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM llm_classification_runs
        WHERE snapshot_id = ?
          AND provider_sha256 = ?
          AND prompt_sha256 = ?
          AND input_sha256 = ?
          AND status = 'complete'
        ORDER BY id DESC
        LIMIT 1
        """,
        (snapshot_id, provider_sha256, prompt_sha256, input_sha256),
    ).fetchone()
    return None if row is None else int(row["id"])


def _validated_batch_json(suggestions: tuple[ClassificationSuggestion, ...]) -> str:
    return json.dumps(
        {"classifications": [item.as_payload() for item in suggestions]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def store_run(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    provider: ProviderConfig,
    prompt: RenderedPrompt,
    videos: list[ClassificationEvidence],
    result: ClassificationRunResult,
    selection_id: int | None = None,
) -> int:
    if len(result.suggestions) != len(videos):
        raise ValueError("cannot store classification run: suggestion/video count mismatch")

    provider_sha, input_sha, cache_key = classification_cache_key(provider, prompt, videos)
    evidence_by_id = {video.video_id: video for video in videos}
    if len(evidence_by_id) != len(videos):
        raise ValueError("cannot store classification run with duplicate video IDs")

    provider_config = {
        "name": provider.name,
        "preset": provider.preset,
        "base_url": provider.base_url,
        "model": provider.model,
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "structured_mode": provider.structured_mode,
        "extra": provider.extra,
    }

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_classification_runs (
                snapshot_id, selection_id, created_at, status,
                provider_name, provider_preset, requested_model,
                provider_sha256, prompt_sha256, input_sha256, cache_key,
                interest_profile, video_count, provider_config_json
            ) VALUES (?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                selection_id,
                _utc_now(),
                provider.name,
                provider.preset,
                provider.model,
                provider_sha,
                prompt.sha256,
                input_sha,
                cache_key,
                prompt.profile_name,
                len(videos),
                json.dumps(provider_config, ensure_ascii=False, sort_keys=True),
            ),
        )
        run_id = int(cursor.lastrowid)

        for batch_index, batch in enumerate(result.batches):
            raw_response = getattr(batch, "raw_response", None)
            batch_cursor = conn.execute(
                """
                INSERT INTO llm_classification_batches (
                    run_id, batch_index, input_sha256, response_model,
                    usage_json, raw_response_json, validated_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    batch_index,
                    batch.input_sha256,
                    batch.response_model,
                    json.dumps(batch.usage, ensure_ascii=False, sort_keys=True),
                    json.dumps(raw_response or {}, ensure_ascii=False, sort_keys=True),
                    _validated_batch_json(batch.suggestions),
                ),
            )
            batch_id = int(batch_cursor.lastrowid)
            for suggestion in batch.suggestions:
                evidence = evidence_by_id[suggestion.video_id]
                conn.execute(
                    """
                    INSERT INTO llm_classifications (
                        run_id, batch_id, video_id, playlist_position,
                        evidence_sha256, evidence_json,
                        action, topic, content_type, timeliness,
                        quality, confidence, reason,
                        existing_playlist, new_queue_proposal,
                        destination_confidence, destination_reason,
                        needs_description, needs_transcript, raw_result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        batch_id,
                        suggestion.video_id,
                        evidence.playlist_position,
                        evidence_item_hash(evidence),
                        json.dumps(
                            evidence.as_payload(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        suggestion.action,
                        suggestion.topic,
                        suggestion.content_type,
                        suggestion.timeliness,
                        suggestion.quality,
                        suggestion.confidence,
                        suggestion.reason,
                        suggestion.existing_playlist,
                        suggestion.new_queue_proposal,
                        suggestion.destination_confidence,
                        suggestion.destination_reason,
                        1 if suggestion.needs_description else 0,
                        1 if suggestion.needs_transcript else 0,
                        json.dumps(
                            suggestion.as_payload(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
    return run_id


def run_payload(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute(
        "SELECT * FROM llm_classification_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise ValueError(f"LLM classification run {run_id} does not exist")

    batches = conn.execute(
        """
        SELECT id, batch_index, input_sha256, response_model, usage_json
        FROM llm_classification_batches
        WHERE run_id = ?
        ORDER BY batch_index
        """,
        (run_id,),
    ).fetchall()
    classifications = conn.execute(
        """
        SELECT c.*, d.action AS current_action,
               d.destination_playlist AS current_destination,
               d.source AS current_decision_source
        FROM llm_classifications AS c
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = ? AND d.video_id = c.video_id
        WHERE c.run_id = ?
        ORDER BY c.playlist_position, c.id
        """,
        (run["snapshot_id"], run_id),
    ).fetchall()

    return {
        "run_id": run_id,
        "snapshot_id": int(run["snapshot_id"]),
        "selection_id": run["selection_id"],
        "created_at": run["created_at"],
        "status": run["status"],
        "provider": run["provider_name"],
        "provider_preset": run["provider_preset"],
        "configured_model": run["requested_model"],
        "provider_sha256": run["provider_sha256"],
        "prompt_sha256": run["prompt_sha256"],
        "input_sha256": run["input_sha256"],
        "cache_key": run["cache_key"],
        "interest_profile": run["interest_profile"],
        "video_count": int(run["video_count"]),
        "batches": [
            {
                "batch_id": int(row["id"]),
                "batch_index": int(row["batch_index"]),
                "input_sha256": row["input_sha256"],
                "response_model": row["response_model"],
                "usage": json.loads(row["usage_json"]),
            }
            for row in batches
        ],
        "classifications": [
            {
                **json.loads(row["raw_result_json"]),
                "evidence_sha256": row["evidence_sha256"],
                "current_decision": None
                if row["current_action"] in (None, "clear")
                else {
                    "action": row["current_action"],
                    "destination_playlist": row["current_destination"],
                    "source": row["current_decision_source"],
                },
            }
            for row in classifications
        ],
    }


def latest_run_id(conn: sqlite3.Connection, snapshot_id: int | None = None) -> int:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    row = conn.execute(
        """
        SELECT id FROM llm_classification_runs
        WHERE snapshot_id = ? AND status = 'complete'
        ORDER BY id DESC LIMIT 1
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"snapshot {snapshot_id} has no stored LLM classification run")
    return int(row["id"])
