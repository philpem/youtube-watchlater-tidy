from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CreatorAssociation:
    key: str
    channel_id: str | None
    name: str


def primary_creator_key(row: Any) -> str:
    return str(
        row["channel_id"]
        or row["uploader_id"]
        or row["channel"]
        or row["uploader"]
        or "(unknown)"
    )


def primary_creator_name(row: Any) -> str:
    return str(row["channel"] or row["uploader"] or "(unknown)")


def primary_creator_names(row: Any) -> set[str]:
    return {
        value.casefold()
        for value in (row["channel"], row["uploader"])
        if isinstance(value, str) and value.strip()
    }


def _raw_creator_names(raw_json: Any) -> list[str]:
    if not isinstance(raw_json, str) or not raw_json:
        return []
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    creators = payload.get("creators")
    if not isinstance(creators, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for value in creators:
        if not isinstance(value, str):
            continue
        name = value.strip()
        folded = name.casefold()
        if not name or folded in seen:
            continue
        seen.add(folded)
        result.append(name)
    return result


def explicit_creator_names(row: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    # Prefer richer metadata names first but retain names present only in the
    # original imported snapshot.
    for field in ("metadata_raw_json", "source_raw_json"):
        for name in _raw_creator_names(row[field]):
            folded = name.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            result.append(name)
    return result


def primary_creator_index(
    rows: Iterable[Any],
) -> tuple[dict[str, set[str]], dict[str, CreatorAssociation]]:
    names_to_keys: dict[str, set[str]] = {}
    details: dict[str, CreatorAssociation] = {}

    for row in rows:
        key = primary_creator_key(row)
        if key == "(unknown)":
            continue
        association = details.setdefault(
            key,
            CreatorAssociation(
                key=key,
                channel_id=row["channel_id"] if isinstance(row["channel_id"], str) else None,
                name=primary_creator_name(row),
            ),
        )
        for value in (row["channel"], row["uploader"]):
            if not isinstance(value, str) or not value.strip():
                continue
            names_to_keys.setdefault(value.casefold(), set()).add(key)
        # Keep the first display name for stable-key grouping, matching the
        # existing report behaviour across channel renames.
        details[key] = association

    return names_to_keys, details


def creator_associations(
    row: Any,
    *,
    names_to_keys: dict[str, set[str]],
    primary_details: dict[str, CreatorAssociation],
) -> list[CreatorAssociation]:
    explicit = explicit_creator_names(row)
    primary_key = primary_creator_key(row)
    result: list[CreatorAssociation] = []
    seen_keys: set[str] = set()

    if primary_key != "(unknown)" or not explicit:
        primary = primary_details.get(
            primary_key,
            CreatorAssociation(
                key=primary_key,
                channel_id=row["channel_id"] if isinstance(row["channel_id"], str) else None,
                name=primary_creator_name(row),
            ),
        )
        result.append(primary)
        seen_keys.add(primary.key)

    primary_names = primary_creator_names(row)
    for name in explicit:
        folded = name.casefold()
        if folded in primary_names:
            continue

        matching_keys = names_to_keys.get(folded, set())
        if len(matching_keys) == 1:
            key = next(iter(matching_keys))
            association = primary_details[key]
        else:
            key = f"name:{folded}"
            association = CreatorAssociation(key=key, channel_id=None, name=name)

        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(association)

    return result


def row_matches_creator(
    row: Any,
    creator: str,
    *,
    names_to_keys: dict[str, set[str]],
) -> bool:
    if primary_creator_key(row) == creator:
        return True

    folded = creator.casefold()
    row_primary_names = primary_creator_names(row)
    if folded in row_primary_names:
        return True

    collaborator_names = {name.casefold() for name in explicit_creator_names(row)}
    if folded in collaborator_names:
        return True

    # Stable-ID selection should include collaboration videos when a collaborator
    # name resolves uniquely to that primary creator elsewhere in the snapshot.
    for collaborator in collaborator_names:
        if names_to_keys.get(collaborator) == {creator}:
            return True
    return False
