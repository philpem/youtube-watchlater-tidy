from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .llm_annotation import AnnotationBatchResult, AnnotationPrompt, AnnotationRunResult
from .llm_classification import ClassificationEvidence, evidence_hash
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


@dataclass(frozen=True)
class ContentFilteredRetryTarget:
    source_run_id: int
    snapshot_id: int
    selection_id: int | None
    taxonomy_source: str
    taxonomy: dict[str, str]
    interest_profile: str | None
    videos: tuple[ClassificationEvidence, ...]


def content_filtered_retry_target(
    conn: sqlite3.Connection,
    run_id: int,
) -> ContentFilteredRetryTarget:
    run = conn.execute(
        """
        SELECT id, snapshot_id, selection_id, taxonomy_source, taxonomy_json,
               interest_profile
        FROM llm_annotation_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"LLM annotation run {run_id} does not exist")

    rows = conn.execute(
        """
        SELECT evidence_json, tags_json
        FROM llm_annotations
        WHERE run_id = ?
        ORDER BY playlist_position, id
        """,
        (run_id,),
    ).fetchall()
    videos: list[ClassificationEvidence] = []
    for row in rows:
        tags = json.loads(row["tags_json"])
        if not isinstance(tags, list) or "content-filtered" not in tags:
            continue
        evidence = json.loads(row["evidence_json"])
        if not isinstance(evidence, dict):
            raise ValueError(
                f"LLM annotation run {run_id} contains invalid stored evidence"
            )
        try:
            videos.append(ClassificationEvidence(**evidence))
        except TypeError as exc:
            raise ValueError(
                f"LLM annotation run {run_id} contains incompatible stored evidence"
            ) from exc

    taxonomy = json.loads(run["taxonomy_json"])
    if not isinstance(taxonomy, dict):
        raise ValueError(f"LLM annotation run {run_id} contains invalid taxonomy")
    return ContentFilteredRetryTarget(
        source_run_id=run_id,
        snapshot_id=int(run["snapshot_id"]),
        selection_id=run["selection_id"],
        taxonomy_source=str(run["taxonomy_source"]),
        taxonomy={str(key): str(value) for key, value in taxonomy.items()},
        interest_profile=run["interest_profile"],
        videos=tuple(videos),
    )


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


def incomplete_annotation_run_id(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    requested_model: str,
    prompt_sha256: str,
    input_sha256: str,
    videos: list[Any],
    batch_size: int,
) -> int | None:
    """Return the newest compatible incomplete run whose stored batches still match.

    Resume identity intentionally ignores execution-only provider settings such as
    streaming and max_tokens. Semantic identity is the requested model, prompt/taxonomy,
    snapshot and complete input evidence hash.
    """

    candidates = conn.execute(
        """
        SELECT id
        FROM llm_annotation_runs
        WHERE snapshot_id = ?
          AND requested_model = ?
          AND prompt_sha256 = ?
          AND input_sha256 = ?
          AND status = 'error'
        ORDER BY id DESC
        """,
        (snapshot_id, requested_model, prompt_sha256, input_sha256),
    ).fetchall()
    batches = [
        videos[index : index + batch_size]
        for index in range(0, len(videos), batch_size)
    ]
    for candidate in candidates:
        run_id = int(candidate["id"])
        stored = conn.execute(
            """
            SELECT batch_index, input_sha256
            FROM llm_annotation_batches
            WHERE run_id = ?
            ORDER BY batch_index
            """,
            (run_id,),
        ).fetchall()
        compatible = True
        for row in stored:
            batch_index = int(row["batch_index"])
            if batch_index < 0 or batch_index >= len(batches):
                compatible = False
                break
            if row["input_sha256"] != evidence_hash(batches[batch_index]):
                compatible = False
                break
        if compatible:
            return run_id
    return None


def annotation_completed_batch_indexes(
    conn: sqlite3.Connection,
    run_id: int,
) -> set[int]:
    return {
        int(row["batch_index"])
        for row in conn.execute(
            "SELECT batch_index FROM llm_annotation_batches WHERE run_id = ?",
            (run_id,),
        )
    }


def _provider_config_payload(
    provider: ProviderConfig,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "name": provider.name,
        "preset": provider.preset,
        "base_url": provider.base_url,
        "model": provider.model,
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "stream": provider.stream,
        "structured_mode": provider.structured_mode,
        "extra": provider.extra,
        "annotation_context": context or {},
    }


def begin_annotation_run(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    provider: ProviderConfig,
    prompt: AnnotationPrompt,
    videos: list[Any],
    taxonomy_source: str,
    selection_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    provider_sha, input_sha, cache_key = annotation_cache_key(provider, prompt, videos)
    provider_config = _provider_config_payload(provider, context)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_annotation_runs (
                snapshot_id, selection_id, created_at, status,
                provider_name, provider_preset, requested_model,
                provider_sha256, prompt_sha256, input_sha256, cache_key,
                interest_profile, taxonomy_source, taxonomy_json,
                video_count, provider_config_json
            ) VALUES (?, ?, ?, 'error', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    return int(cursor.lastrowid)


def store_annotation_batch(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    batch_index: int,
    videos: list[Any],
    result: AnnotationBatchResult,
) -> None:
    if len(result.annotations) != len(videos):
        raise ValueError("cannot checkpoint annotation batch: annotation/video count mismatch")
    evidence_by_id = {video.video_id: video for video in videos}
    if len(evidence_by_id) != len(videos):
        raise ValueError("cannot checkpoint annotation batch with duplicate video IDs")
    annotation_ids = {annotation.video_id for annotation in result.annotations}
    if annotation_ids != set(evidence_by_id):
        raise ValueError("cannot checkpoint annotation batch: annotation/video IDs mismatch")
    expected_input_sha = evidence_hash(videos)
    if result.input_sha256 != expected_input_sha:
        raise ValueError("cannot checkpoint annotation batch: input hash mismatch")

    with conn:
        existing = conn.execute(
            """
            SELECT 1 FROM llm_annotation_batches
            WHERE run_id = ? AND batch_index = ?
            """,
            (run_id, batch_index),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"annotation run {run_id} batch {batch_index} is already checkpointed"
            )
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
                result.input_sha256,
                result.response_model,
                json.dumps(result.usage, ensure_ascii=False, sort_keys=True),
                _validated_batch_json(result.annotations),
            ),
        )
        batch_id = int(batch_cursor.lastrowid)
        for annotation in result.annotations:
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


def complete_annotation_run(conn: sqlite3.Connection, run_id: int) -> None:
    row = conn.execute(
        """
        SELECT r.video_count, COUNT(a.id) AS stored_count
        FROM llm_annotation_runs AS r
        LEFT JOIN llm_annotations AS a ON a.run_id = r.id
        WHERE r.id = ?
        GROUP BY r.id
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"LLM annotation run {run_id} does not exist")
    expected = int(row["video_count"])
    stored = int(row["stored_count"])
    if stored != expected:
        raise ValueError(
            f"cannot complete annotation run {run_id}: "
            f"{stored}/{expected} annotations are checkpointed"
        )
    with conn:
        conn.execute(
            "UPDATE llm_annotation_runs SET status = 'complete' WHERE id = ?",
            (run_id,),
        )


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
    """Store an already-complete in-memory result.

    Kept for library callers/tests; the CLI uses begin/checkpoint/complete so
    long-running annotation work is durable as each batch validates.
    """
    if len(result.annotations) != len(videos):
        raise ValueError("cannot store annotation run: annotation/video count mismatch")
    run_id = begin_annotation_run(
        conn,
        snapshot_id=snapshot_id,
        selection_id=selection_id,
        provider=provider,
        prompt=prompt,
        videos=videos,
        taxonomy_source=taxonomy_source,
        context=context,
    )
    offset = 0
    for batch_index, batch in enumerate(result.batches):
        batch_videos = videos[offset : offset + len(batch.annotations)]
        normalized_batch = AnnotationBatchResult(
            annotations=batch.annotations,
            input_sha256=evidence_hash(batch_videos),
            usage=batch.usage,
            response_model=batch.response_model,
        )
        store_annotation_batch(
            conn,
            run_id=run_id,
            batch_index=batch_index,
            videos=batch_videos,
            result=normalized_batch,
        )
        offset += len(batch.annotations)
    complete_annotation_run(conn, run_id)
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
        "stored_video_count": len(annotations),
        "batch_count": len(batches),
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
