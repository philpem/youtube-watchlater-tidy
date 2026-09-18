from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .llm_annotation import AnnotationPrompt, AnnotationRunResult
from .llm_classification import evidence_hash
from .llm_config import ProviderConfig
from .llm_store import evidence_item_hash, provider_fingerprint
from .reports import latest_snapshot_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def annotation_cache_key(
    provider: ProviderConfig,
    prompt: AnnotationPrompt,
    videos: list[Any],
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


def cached_annotation_run_id(
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
        FROM llm_annotation_runs
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


def _validated_batch_json(annotations: tuple[Any, ...]) -> str:
    return json.dumps(
        {"annotations": [item.as_payload() for item in annotations]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def store_annotation_run(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    provider: ProviderConfig,
    prompt: AnnotationPrompt,
    videos: list[Any],
    result: AnnotationRunResult,
    taxonomy_source: str,
    selection_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    if len(result.annotations) != len(videos):
        raise ValueError("cannot store annotation run: annotation/video count mismatch")

    provider_sha, input_sha, cache_key = annotation_cache_key(provider, prompt, videos)
    evidence_by_id = {video.video_id: video for video in videos}
    if len(evidence_by_id) != len(videos):
        raise ValueError("cannot store annotation run with duplicate video IDs")

    provider_config = {
        "name": provider.name,
        "preset": provider.preset,
        "base_url": provider.base_url,
        "model": provider.model,
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "structured_mode": provider.structured_mode,
        "extra": provider.extra,
        "annotation_context": context or {},
    }

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_annotation_runs (
                snapshot_id, selection_id, created_at, status,
                provider_name, provider_preset, requested_model,
                provider_sha256, prompt_sha256, input_sha256, cache_key,
                interest_profile, taxonomy_source, taxonomy_json,
                video_count, provider_config_json
            ) VALUES (?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                taxonomy_source,
                json.dumps(prompt.categories, ensure_ascii=False, sort_keys=True),
                len(videos),
                json.dumps(provider_config, ensure_ascii=False, sort_keys=True),
            ),
        )
        run_id = int(cursor.lastrowid)

        for batch_index, batch in enumerate(result.batches):
            batch_cursor = conn.execute(
                """
                INSERT INTO llm_annotation_batches (
                    run_id, batch_index, input_sha256, response_model,
                    usage_json, raw_response_json, validated_json
                ) VALUES (?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    run_id,
                    batch_index,
                    batch.input_sha256,
                    batch.response_model,
                    json.dumps(batch.usage, ensure_ascii=False, sort_keys=True),
                    _validated_batch_json(batch.annotations),
                ),
            )
            batch_id = int(batch_cursor.lastrowid)
            for annotation in batch.annotations:
                evidence = evidence_by_id[annotation.video_id]
                conn.execute(
                    """
                    INSERT INTO llm_annotations (
                        run_id, batch_id, video_id, playlist_position,
                        evidence_sha256, evidence_json,
                        primary_category, subject, tags_json,
                        content_type, confidence, raw_result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        batch_id,
                        annotation.video_id,
                        evidence.playlist_position,
                        evidence_item_hash(evidence),
                        json.dumps(
                            evidence.as_payload(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        annotation.primary_category,
                        annotation.subject,
                        json.dumps(list(annotation.tags), ensure_ascii=False),
                        annotation.content_type,
                        annotation.confidence,
                        json.dumps(
                            annotation.as_payload(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
    return run_id


def annotation_run_payload(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute(
        "SELECT * FROM llm_annotation_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise ValueError(f"LLM annotation run {run_id} does not exist")

    provider_config = json.loads(run["provider_config_json"])
    context = provider_config.get("annotation_context", {})
    if not isinstance(context, dict):
        context = {}

    batches = conn.execute(
        """
        SELECT id, batch_index, input_sha256, response_model, usage_json
        FROM llm_annotation_batches
        WHERE run_id = ?
        ORDER BY batch_index
        """,
        (run_id,),
    ).fetchall()
    annotations = conn.execute(
        """
        SELECT a.*, d.action AS current_action,
               d.destination_playlist AS current_destination,
               d.source AS current_decision_source
        FROM llm_annotations AS a
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = ? AND d.video_id = a.video_id
        WHERE a.run_id = ?
        ORDER BY a.playlist_position, a.id
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
        "taxonomy_source": run["taxonomy_source"],
        "taxonomy": json.loads(run["taxonomy_json"]),
        "video_count": int(run["video_count"]),
        "context": context,
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
        "annotations": [
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
            for row in annotations
        ],
    }


def latest_annotation_run_id(
    conn: sqlite3.Connection, snapshot_id: int | None = None
) -> int:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    row = conn.execute(
        """
        SELECT id FROM llm_annotation_runs
        WHERE snapshot_id = ? AND status = 'complete'
        ORDER BY id DESC LIMIT 1
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"snapshot {snapshot_id} has no stored LLM annotation run")
    return int(row["id"])
