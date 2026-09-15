from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .reports import latest_snapshot_id
from .triage import ACTIONS, select_creator, select_title


@dataclass(frozen=True)
class SavedRule:
    id: int
    name: str
    enabled: bool
    priority: int
    selector_type: str
    selector: dict[str, Any]
    action: str
    destination_playlist: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RuleApplyResult:
    rule_id: int
    name: str
    selection_id: int
    matched: int
    applied: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_action(action: str, destination_playlist: str | None) -> None:
    if action not in ACTIONS:
        raise ValueError(f"invalid action {action!r}; expected one of {', '.join(ACTIONS)}")
    if action == "move" and not destination_playlist:
        raise ValueError("move rules require a destination playlist")
    if action != "move" and destination_playlist:
        raise ValueError("a destination playlist is only valid for move rules")


def _row_to_rule(row: sqlite3.Row) -> SavedRule:
    return SavedRule(
        id=int(row["id"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        priority=int(row["priority"]),
        selector_type=str(row["selector_type"]),
        selector=json.loads(row["selector_json"]),
        action=str(row["action"]),
        destination_playlist=row["destination_playlist"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def saved_rules(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[SavedRule]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"""
        SELECT id, name, enabled, priority, selector_type, selector_json,
               action, destination_playlist, created_at, updated_at
        FROM saved_rules
        {where}
        ORDER BY priority, id
        """
    ).fetchall()
    return [_row_to_rule(row) for row in rows]


def get_rule(conn: sqlite3.Connection, rule_id: int) -> SavedRule:
    row = conn.execute(
        """
        SELECT id, name, enabled, priority, selector_type, selector_json,
               action, destination_playlist, created_at, updated_at
        FROM saved_rules
        WHERE id = ?
        """,
        (rule_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"rule {rule_id} does not exist")
    return _row_to_rule(row)


def save_rule_from_selection(
    conn: sqlite3.Connection,
    selection_id: int,
    *,
    name: str,
    action: str,
    destination_playlist: str | None = None,
    priority: int = 100,
) -> int:
    _validate_action(action, destination_playlist)
    if not name.strip():
        raise ValueError("rule name cannot be empty")

    selection = conn.execute(
        """
        SELECT selector_type, selector_json
        FROM selections
        WHERE id = ?
        """,
        (selection_id,),
    ).fetchone()
    if selection is None:
        raise ValueError(f"selection {selection_id} does not exist")
    selector_type = str(selection["selector_type"])
    if selector_type not in ("creator", "title"):
        raise ValueError(f"selection type {selector_type!r} cannot be saved as a reusable rule")

    selector = json.loads(selection["selector_json"])
    # Reusable rules always operate against the unresolved remainder of the
    # target snapshot, regardless of how the original preview was created.
    selector["remaining"] = True
    now = _utc_now()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO saved_rules (
                    name, enabled, priority, selector_type, selector_json,
                    action, destination_playlist, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    priority,
                    selector_type,
                    json.dumps(selector, ensure_ascii=False, sort_keys=True),
                    action,
                    destination_playlist,
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        if "saved_rules.name" in str(exc) or "UNIQUE" in str(exc):
            raise ValueError(f"a rule named {name.strip()!r} already exists") from exc
        raise
    return int(cursor.lastrowid)


def set_rule_enabled(conn: sqlite3.Connection, rule_id: int, enabled: bool) -> None:
    with conn:
        cursor = conn.execute(
            "UPDATE saved_rules SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _utc_now(), rule_id),
        )
    if cursor.rowcount == 0:
        raise ValueError(f"rule {rule_id} does not exist")


def _select_for_rule(
    conn: sqlite3.Connection,
    rule: SavedRule,
    snapshot_id: int,
):
    selector = dict(rule.selector)
    common = {
        "snapshot_id": snapshot_id,
        "remaining": True,
        "min_duration": selector.get("min_duration"),
        "max_duration": selector.get("max_duration"),
        "min_position": selector.get("min_position"),
        "max_position": selector.get("max_position"),
    }
    if rule.selector_type == "creator":
        return select_creator(conn, str(selector["creator"]), **common)
    if rule.selector_type == "title":
        return select_title(
            conn,
            contains=selector.get("contains"),
            regex=selector.get("regex"),
            case_sensitive=bool(selector.get("case_sensitive", False)),
            **common,
        )
    raise ValueError(f"unsupported saved rule selector type {rule.selector_type!r}")


def apply_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    snapshot_id: int | None = None,
    *,
    commit: bool = True,
) -> RuleApplyResult:
    rule = get_rule(conn, rule_id)
    if not rule.enabled:
        raise ValueError(f"rule {rule_id} ({rule.name}) is disabled")
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    selection = _select_for_rule(conn, rule, snapshot_id)
    if not commit:
        return RuleApplyResult(rule.id, rule.name, selection.selection_id, selection.entry_count, 0)

    selected = conn.execute(
        "SELECT video_id FROM selection_entries WHERE selection_id = ? ORDER BY video_id",
        (selection.selection_id,),
    ).fetchall()
    now = _utc_now()
    provenance = json.dumps(
        {"rule_id": rule.id, "rule_name": rule.name, "priority": rule.priority},
        ensure_ascii=False,
        sort_keys=True,
    )
    applied = 0
    with conn:
        for row in selected:
            current = conn.execute(
                """
                SELECT id, action
                FROM current_decisions
                WHERE snapshot_id = ? AND video_id = ?
                """,
                (snapshot_id, row["video_id"]),
            ).fetchone()
            if current is not None and current["action"] not in (None, "clear"):
                continue
            previous_id = current["id"] if current is not None else None
            conn.execute(
                """
                INSERT INTO decision_events (
                    snapshot_id, video_id, action, destination_playlist,
                    source, rule_json, reason, created_at, supersedes_id
                ) VALUES (?, ?, ?, ?, 'rule', ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    row["video_id"],
                    rule.action,
                    rule.destination_playlist,
                    provenance,
                    f"saved rule: {rule.name}",
                    now,
                    previous_id,
                ),
            )
            applied += 1

    return RuleApplyResult(rule.id, rule.name, selection.selection_id, selection.entry_count, applied)


def apply_enabled_rules(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    commit: bool = True,
) -> list[RuleApplyResult]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    results: list[RuleApplyResult] = []
    for rule in saved_rules(conn, enabled_only=True):
        results.append(apply_rule(conn, rule.id, snapshot_id, commit=commit))
    return results
