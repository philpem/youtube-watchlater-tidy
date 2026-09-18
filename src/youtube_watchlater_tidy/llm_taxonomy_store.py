from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .llm_annotation import TaxonomyDiscovery
from .llm_config import ProviderConfig
from .llm_store import provider_fingerprint


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_taxonomy(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    selection_id: int | None,
    provider: ProviderConfig,
    discovery: TaxonomyDiscovery,
    sample_count: int,
    max_categories: int,
    interest_profile: str | None,
) -> int:
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_taxonomies (
                snapshot_id, selection_id, created_at,
                provider_name, provider_preset, requested_model,
                provider_sha256, interest_profile,
                prompt_sha256, input_sha256,
                sample_count, max_categories,
                categories_json, usage_json, response_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                selection_id,
                _utc_now(),
                provider.name,
                provider.preset,
                provider.model,
                provider_fingerprint(provider),
                interest_profile,
                discovery.prompt_sha256,
                discovery.input_sha256,
                sample_count,
                max_categories,
                json.dumps(discovery.categories, ensure_ascii=False, sort_keys=True),
                json.dumps(discovery.usage, ensure_ascii=False, sort_keys=True),
                discovery.response_model,
            ),
        )
    return int(cursor.lastrowid)


def taxonomy_payload(conn: sqlite3.Connection, taxonomy_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM llm_taxonomies WHERE id = ?",
        (taxonomy_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"saved taxonomy {taxonomy_id} does not exist")
    categories = json.loads(row["categories_json"])
    if not isinstance(categories, dict):
        raise ValueError(f"saved taxonomy {taxonomy_id} has invalid categories")
    return {
        "taxonomy_id": int(row["id"]),
        "snapshot_id": int(row["snapshot_id"]),
        "selection_id": row["selection_id"],
        "created_at": row["created_at"],
        "provider": row["provider_name"],
        "provider_preset": row["provider_preset"],
        "configured_model": row["requested_model"],
        "provider_sha256": row["provider_sha256"],
        "interest_profile": row["interest_profile"],
        "prompt_sha256": row["prompt_sha256"],
        "input_sha256": row["input_sha256"],
        "sample_count": int(row["sample_count"]),
        "max_categories": int(row["max_categories"]),
        "category_count": len(categories),
        "categories": categories,
        "usage": json.loads(row["usage_json"]),
        "response_model": row["response_model"],
    }


def taxonomy_categories(conn: sqlite3.Connection, taxonomy_id: int) -> dict[str, str]:
    payload = taxonomy_payload(conn, taxonomy_id)
    result: dict[str, str] = {}
    for name, description in payload["categories"].items():
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError(f"saved taxonomy {taxonomy_id} contains invalid category data")
        result[name] = description
    return result


def taxonomy_list_payload(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, snapshot_id, selection_id, created_at,
               provider_name, requested_model, interest_profile,
               sample_count, max_categories, categories_json, response_model
        FROM llm_taxonomies
    """
    params: tuple[Any, ...] = ()
    if snapshot_id is not None:
        sql += " WHERE snapshot_id = ?"
        params = (snapshot_id,)
    sql += " ORDER BY id DESC"

    result: list[dict[str, Any]] = []
    for row in conn.execute(sql, params):
        categories = json.loads(row["categories_json"])
        result.append(
            {
                "taxonomy_id": int(row["id"]),
                "snapshot_id": int(row["snapshot_id"]),
                "selection_id": row["selection_id"],
                "created_at": row["created_at"],
                "provider": row["provider_name"],
                "configured_model": row["requested_model"],
                "interest_profile": row["interest_profile"],
                "sample_count": int(row["sample_count"]),
                "max_categories": int(row["max_categories"]),
                "category_count": len(categories) if isinstance(categories, dict) else 0,
                "response_model": row["response_model"],
            }
        )
    return result
