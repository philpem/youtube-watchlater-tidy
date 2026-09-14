from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from .reports import _effective_rows, latest_snapshot_id

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#._-]*")

# Deliberately small and unsurprising. Technical terms are not stemmed or
# normalised beyond case-folding, so C++, V.34, Z80, ESPHome, etc. survive.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "i", "in", "is", "it", "of", "on", "or", "the", "this", "to",
    "vs", "with", "you", "your",
}


@dataclass(frozen=True)
class KeywordRow:
    phrase: str
    count: int
    first_position: int
    last_position: int
    examples: tuple[str, ...]


def _tokens(title: str) -> list[str]:
    return [match.group(0).casefold() for match in TOKEN_RE.finditer(title)]


def _ngrams(tokens: list[str], size: int) -> set[str]:
    phrases: set[str] = set()
    for start in range(0, len(tokens) - size + 1):
        words = tokens[start : start + size]
        if size == 1:
            if words[0] in STOPWORDS:
                continue
        else:
            # Reject boilerplate fragments such as "how to" / "the best" but
            # retain useful phrases with internal glue words.
            if words[0] in STOPWORDS or words[-1] in STOPWORDS:
                continue
            if all(word in STOPWORDS for word in words):
                continue
        phrase = " ".join(words)
        if len(phrase) < 2:
            continue
        phrases.add(phrase)
    return phrases


def keyword_rows(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    remaining: bool = False,
    ngram: int = 1,
    min_count: int = 2,
    max_examples: int = 2,
) -> list[KeywordRow]:
    if ngram not in (1, 2, 3):
        raise ValueError("--ngram must be 1, 2, or 3")
    if min_count < 1:
        raise ValueError("--min-count must be at least 1")
    if max_examples < 0:
        raise ValueError("--examples cannot be negative")
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    positions: dict[str, list[int]] = defaultdict(list)
    examples: dict[str, list[str]] = defaultdict(list)

    for row in _effective_rows(conn, snapshot_id):
        action = None if row["current_action"] == "clear" else row["current_action"]
        if remaining and action is not None:
            continue
        title = str(row["title"] or "").strip()
        if not title or title.casefold() in {"[private video]", "[deleted video]"}:
            continue

        for phrase in _ngrams(_tokens(title), ngram):
            positions[phrase].append(int(row["position"]))
            if len(examples[phrase]) < max_examples and title not in examples[phrase]:
                examples[phrase].append(title)

    result = [
        KeywordRow(
            phrase=phrase,
            count=len(phrase_positions),
            first_position=min(phrase_positions),
            last_position=max(phrase_positions),
            examples=tuple(examples[phrase]),
        )
        for phrase, phrase_positions in positions.items()
        if len(phrase_positions) >= min_count
    ]
    result.sort(key=lambda row: (-row.count, row.phrase))
    return result


def render_keywords(rows: list[KeywordRow], limit: int | None = None) -> str:
    total = len(rows)
    shown = rows if limit is None else rows[:limit]
    headers = ["Count", "Positions", "Keyword / phrase", "Examples"]
    data = [
        [
            str(row.count),
            f"{row.first_position}-{row.last_position}",
            row.phrase,
            " | ".join(row.examples),
        ]
        for row in shown
    ]

    widths = [len(header) for header in headers]
    for item in data:
        for index, value in enumerate(item):
            widths[index] = max(widths[index], len(value))

    def line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values)).rstrip()

    output = [line(headers), line(["-" * width for width in widths])]
    output.extend(line(item) for item in data)
    if limit is not None and total > limit:
        output.append(f"... {total - limit} more")
    output.append(f"{total} keyword/phrase(s)")
    return "\n".join(output)
