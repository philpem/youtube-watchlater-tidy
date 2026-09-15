from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from .reports import _effective_rows, latest_snapshot_id

# Keep punctuation that is often semantically meaningful in technical terms
# (C++, V.34, ESPHome, 3D, etc.) and keep apostrophes inside contractions so
# "don't" does not turn into the meaningless bigram "don t".
TOKEN_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9+#._-]*(?:['’][A-Za-z0-9][A-Za-z0-9+#._-]*)*"
)

# Common function words and contraction forms. These are deliberately used for
# keyword discovery only; titles themselves are never rewritten. Technical
# terms are not stemmed or otherwise normalised beyond case-folding.
COMMON_WORDS = {
    "a", "about", "after", "all", "also", "am", "an", "and", "any", "are",
    "aren't", "as", "at", "be", "been", "being", "but", "by", "can", "can't",
    "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "don't",
    "for", "from", "had", "has", "have", "haven't", "he", "her", "here",
    "here's", "hers", "him", "his", "how", "i", "i'd", "i'll", "i'm", "i've",
    "if", "in", "into", "is", "isn't", "it", "it's", "its", "let", "let's",
    "me", "more", "most", "my", "no", "not", "of", "on", "one", "or", "our",
    "ours", "people", "r", "she", "should", "so", "some", "than", "that",
    "that's", "the", "their", "them", "then", "there", "there's", "these",
    "they", "they're", "this", "those", "to", "too", "us", "ve", "vs", "was",
    "wasn't", "we", "we're", "were", "weren't", "what", "what's", "when",
    "where", "which", "who", "why", "will", "with", "won't", "would", "you",
    "you'd", "you'll", "you're", "you've", "your", "yours",
}

# Recurring title structure that is usually less useful than topical phrases.
SERIES_WORDS = {"part", "pt", "episode", "ep", "chapter"}


@dataclass(frozen=True)
class KeywordRow:
    phrase: str
    count: int
    first_position: int
    last_position: int
    examples: tuple[str, ...]


def _tokens(title: str) -> list[str]:
    return [
        match.group(0).replace("’", "'").casefold()
        for match in TOKEN_RE.finditer(title)
    ]


def _is_series_fragment(words: list[str]) -> bool:
    if len(words) < 2 or words[0] not in SERIES_WORDS:
        return False
    suffix = words[1].rstrip(".:")
    return suffix.isdigit() or re.fullmatch(r"[ivxlcdm]+", suffix, re.IGNORECASE) is not None


def _ngrams(tokens: list[str], size: int, *, include_common: bool = False) -> set[str]:
    phrases: set[str] = set()
    for start in range(0, len(tokens) - size + 1):
        words = tokens[start : start + size]

        if not include_common:
            if size == 1:
                if words[0] in COMMON_WORDS:
                    continue
            else:
                # Low-information n-grams tend to start or end in a function
                # word ("what happens", "truth about", "people who", ...).
                # Glue words are still allowed internally, e.g. "history of x".
                if words[0] in COMMON_WORDS or words[-1] in COMMON_WORDS:
                    continue
                if all(word in COMMON_WORDS for word in words):
                    continue
                if _is_series_fragment(words):
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
    include_common: bool = False,
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

        for phrase in _ngrams(_tokens(title), ngram, include_common=include_common):
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
