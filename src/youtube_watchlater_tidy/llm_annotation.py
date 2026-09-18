from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .llm_classification import ClassificationEvidence, evidence_hash
from .llm_config import ProjectConfig, ProviderConfig
from .llm_parallel import run_bounded_parallel
from .llm_prompt import render_prompt
from .llm_provider import ChatResponse, chat, merge_usage, parse_json_content
from .progress import ProgressCallback, ProgressEvent


RESERVED_CATEGORIES = {
    "Other": "Material that does not fit another configured category.",
    "Unclear": "Insufficient evidence to assign a useful semantic category.",
}

ANNOTATION_SYSTEM_PROMPT = """You organise a user's YouTube Watch Later catalogue for human review.

This task is semantic annotation, not decision making. Do not recommend keep/delete/archive/move
actions. For each supplied video:
- choose exactly one primary category from the supplied controlled vocabulary;
- write a short, specific subject describing what the video is about;
- assign 1 to 6 concise reusable tags;
- identify the content type (for example tutorial, technical talk, repair, review, news, comedy);
- give confidence in the semantic annotation.

Tags must describe subject matter, not quality or recommended action. Prefer stable canonical names
and lower-case tags. Avoid generic tags such as "video", "youtube", or "technology" when a more
specific tag is available. Reuse the same tag wording for the same concept across videos.

Use Other when the subject genuinely falls outside the vocabulary. Use Unclear when the available
metadata is insufficient. Return only JSON matching the supplied schema. The video_id in each
result must exactly match one supplied ID.
"""

TAXONOMY_SYSTEM_PROMPT = """Design a broad controlled category vocabulary for browsing a large
YouTube Watch Later catalogue. Categories are navigation facets for human review, not quality
judgements or recommended actions.

Create a manageable set of broad, reusable categories. Merge near-synonyms and avoid categories
that are merely one channel name, one individual video, a quality judgement, or a transient action.
Names should normally be 1 to 4 words. Descriptions should clarify boundaries. Do not include
Other or Unclear; the application adds those reserved fallbacks.

Return only JSON matching the supplied schema.
"""

TAXONOMY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["categories"],
    "properties": {
        "categories": {
            "type": "array",
            "minItems": 3,
            "maxItems": 30,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        }
    },
}


@dataclass(frozen=True)
class AnnotationPrompt:
    system: str
    interest_brief: str
    profile_name: str | None
    categories: dict[str, str]
    sha256: str

    def schema(self) -> dict[str, Any]:
        return annotation_schema(self.categories)

    def messages(self) -> list[dict[str, str]]:
        vocabulary = "\n".join(
            f"- {name}: {description}" for name, description in self.categories.items()
        )
        user_sections = [
            "## Controlled category vocabulary\n" + vocabulary,
            "## User interest context\n"
            + (
                self.interest_brief
                or "No interest profile supplied. Categorise by subject matter only."
            ),
            "## Required annotation JSON schema\n"
            + json.dumps(self.schema(), ensure_ascii=False, sort_keys=True),
        ]
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": "\n\n".join(user_sections)},
        ]


@dataclass(frozen=True)
class SemanticAnnotation:
    video_id: str
    primary_category: str
    subject: str
    tags: tuple[str, ...]
    content_type: str
    confidence: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "primary_category": self.primary_category,
            "subject": self.subject,
            "tags": list(self.tags),
            "content_type": self.content_type,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class AnnotationBatchResult:
    annotations: tuple[SemanticAnnotation, ...]
    input_sha256: str
    usage: dict[str, Any]
    response_model: str | None


@dataclass(frozen=True)
class AnnotationRunResult:
    annotations: tuple[SemanticAnnotation, ...]
    batches: tuple[AnnotationBatchResult, ...]


@dataclass(frozen=True)
class TaxonomyDiscovery:
    categories: dict[str, str]
    input_sha256: str
    prompt_sha256: str
    usage: dict[str, Any]
    response_model: str | None

    def as_context(self) -> dict[str, Any]:
        return {
            "input_sha256": self.input_sha256,
            "prompt_sha256": self.prompt_sha256,
            "usage": self.usage,
            "response_model": self.response_model,
        }


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def with_reserved_categories(categories: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    reserved_by_fold = {name.casefold(): name for name in RESERVED_CATEGORIES}
    reserved_descriptions: dict[str, str] = dict(RESERVED_CATEGORIES)
    seen: set[str] = set()

    for raw_name, raw_description in categories.items():
        name = str(raw_name).strip()
        description = str(raw_description).strip()
        if not name or not description:
            raise ValueError("review category names and descriptions must be non-empty")
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"duplicate review category {name!r}")
        seen.add(folded)
        if folded in reserved_by_fold:
            canonical = reserved_by_fold[folded]
            reserved_descriptions[canonical] = description
        else:
            result[name] = description

    for name, description in reserved_descriptions.items():
        result[name] = description
    return result


def annotation_schema(categories: dict[str, str]) -> dict[str, Any]:
    names = list(categories)
    if not names:
        raise ValueError("annotation category vocabulary cannot be empty")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["annotations"],
        "properties": {
            "annotations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "video_id",
                        "primary_category",
                        "subject",
                        "tags",
                        "content_type",
                        "confidence",
                    ],
                    "properties": {
                        "video_id": {"type": "string"},
                        "primary_category": {"type": "string", "enum": names},
                        "subject": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                        "content_type": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                },
            }
        },
    }


def render_annotation_prompt(
    config: ProjectConfig,
    categories: dict[str, str],
    *,
    interest_profile: str | None = None,
    prompt_file: str | Path | None = None,
) -> AnnotationPrompt:
    base = render_prompt(
        config,
        interest_profile=interest_profile,
        prompt_file=prompt_file,
    )
    controlled = with_reserved_categories(categories)
    schema = annotation_schema(controlled)
    digest = _canonical_hash(
        {
            "prompt_version": 1,
            "system": ANNOTATION_SYSTEM_PROMPT,
            "interest": base.interest_brief,
            "categories": controlled,
            "schema": schema,
        }
    )
    return AnnotationPrompt(
        system=ANNOTATION_SYSTEM_PROMPT,
        interest_brief=base.interest_brief,
        profile_name=base.profile_name,
        categories=controlled,
        sha256=digest,
    )


def build_annotation_messages(
    prompt: AnnotationPrompt,
    videos: list[ClassificationEvidence],
) -> list[dict[str, str]]:
    messages = prompt.messages()
    messages.append(
        {
            "role": "user",
            "content": "## Videos to annotate\n"
            + json.dumps(
                {"videos": [video.as_payload() for video in videos]},
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
    )
    return messages


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"annotation field {field} must be a non-empty string")
    return value.strip()


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"annotation field {field} must be a number from 0 to 1")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"annotation field {field} must be between 0 and 1")
    return result


def _normalise_tags(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 6:
        raise ValueError(f"annotation field {field} must contain 1 to 6 tags")
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        tag = _string(raw, f"{field}[{index}]").lstrip("#")
        tag = " ".join(tag.casefold().split())
        if not tag:
            raise ValueError(f"annotation field {field}[{index}] must not be empty")
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    if not result:
        raise ValueError(f"annotation field {field} must contain at least one tag")
    return tuple(result)


def validate_annotation_response(
    value: dict[str, Any],
    *,
    expected_video_ids: list[str],
    categories: set[str],
) -> list[SemanticAnnotation]:
    if set(value) != {"annotations"}:
        raise ValueError("annotation response must contain only 'annotations'")
    rows = value.get("annotations")
    if not isinstance(rows, list):
        raise ValueError("annotation response 'annotations' must be an array")

    expected = list(expected_video_ids)
    expected_set = set(expected)
    if len(expected_set) != len(expected):
        raise ValueError("expected video IDs contain duplicates")

    required = {
        "video_id",
        "primary_category",
        "subject",
        "tags",
        "content_type",
        "confidence",
    }
    annotations: dict[str, SemanticAnnotation] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError(f"annotation[{index}] has missing or unexpected fields")
        video_id = _string(row["video_id"], f"annotations[{index}].video_id")
        if video_id not in expected_set:
            raise ValueError(f"annotation returned unexpected video_id {video_id!r}")
        if video_id in annotations:
            raise ValueError(f"annotation returned duplicate video_id {video_id!r}")

        category = _string(row["primary_category"], f"{video_id}.primary_category")
        if category not in categories:
            raise ValueError(
                f"{video_id}.primary_category {category!r} is not in the controlled vocabulary"
            )
        annotations[video_id] = SemanticAnnotation(
            video_id=video_id,
            primary_category=category,
            subject=_string(row["subject"], f"{video_id}.subject"),
            tags=_normalise_tags(row["tags"], f"{video_id}.tags"),
            content_type=_string(row["content_type"], f"{video_id}.content_type"),
            confidence=_score(row["confidence"], f"{video_id}.confidence"),
        )

    missing = [video_id for video_id in expected if video_id not in annotations]
    if missing:
        raise ValueError("annotation response omitted video ID(s): " + ", ".join(missing))
    return [annotations[video_id] for video_id in expected]


def _batches(
    videos: list[ClassificationEvidence], batch_size: int
) -> list[list[ClassificationEvidence]]:
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    return [videos[index : index + batch_size] for index in range(0, len(videos), batch_size)]


def _request_annotation_batch(
    provider: ProviderConfig,
    prompt: AnnotationPrompt,
    videos: list[ClassificationEvidence],
    progress: ProgressCallback | None,
    phase: str,
) -> ChatResponse:
    chat_kwargs: dict[str, Any] = {"json_schema": prompt.schema()}
    if progress is not None:
        chat_kwargs.update({"progress": progress, "phase": phase})
    return chat(
        provider,
        build_annotation_messages(prompt, videos),
        **chat_kwargs,
    )


def annotate(
    provider: ProviderConfig,
    prompt: AnnotationPrompt,
    videos: list[ClassificationEvidence],
    *,
    batch_size: int = 10,
    progress: ProgressCallback | None = None,
    phase: str = "LLM annotation",
    completed_batch_indexes: set[int] | None = None,
    on_batch: Callable[
        [int, list[ClassificationEvidence], AnnotationBatchResult], None
    ]
    | None = None,
) -> AnnotationRunResult:
    batches = _batches(videos, batch_size)
    if not batches:
        return AnnotationRunResult(annotations=(), batches=())

    completed = set(completed_batch_indexes or ())
    invalid = sorted(index for index in completed if index < 0 or index >= len(batches))
    if invalid:
        raise ValueError(
            "completed annotation batch index(es) out of range: "
            + ", ".join(str(index) for index in invalid)
        )
    pending = [
        (index, batch)
        for index, batch in enumerate(batches)
        if index not in completed
    ]

    completed_videos = sum(len(batches[index]) for index in completed)
    completed_batches = len(completed)
    responses_received = 0

    if progress is not None:
        checkpoint_text = (
            f"; {len(completed)} checkpointed"
            if completed
            else ""
        )
        progress(
            ProgressEvent(
                phase=phase,
                kind="start",
                completed=completed_videos,
                total=len(videos),
                unit="video",
                detail=(
                    f"{len(batches)} batch(es); batch_size={batch_size}; "
                    f"concurrency={provider.concurrency}{checkpoint_text}"
                ),
            )
        )
        in_flight = min(provider.concurrency, len(pending))
        queued = max(0, len(pending) - in_flight)
        detail = (
            f"{in_flight} request(s) in flight; {queued} queued; "
            "waiting for first response"
            if pending
            else "all batches already checkpointed"
        )
        progress(
            ProgressEvent(
                phase=phase,
                kind="status",
                completed=completed_videos,
                total=len(videos),
                unit="video",
                detail=detail,
            )
        )

    def note_response(label: str) -> None:
        nonlocal responses_received
        responses_received += 1
        if progress is not None:
            progress(
                ProgressEvent(
                    phase=phase,
                    kind="status",
                    completed=completed_videos,
                    total=len(videos),
                    unit="video",
                    detail=f"response {responses_received} received; validating {label}",
                )
            )

    def request(
        task_index: int,
        item: tuple[int, list[ClassificationEvidence]],
    ) -> ChatResponse:
        _original_index, batch = item
        return _request_annotation_batch(provider, prompt, batch, progress, phase)

    def on_response(
        task_index: int,
        item: tuple[int, list[ClassificationEvidence]],
        response: ChatResponse,
    ) -> None:
        original_index, _batch = item
        note_response(f"batch {original_index + 1}")

    def validate_batch(
        label: str,
        batch: list[ClassificationEvidence],
        response: ChatResponse,
    ) -> AnnotationBatchResult:
        if response.finish_reason in {"length", "content_filter"}:
            if len(batch) == 1:
                if response.finish_reason == "length":
                    error = RuntimeError(
                        f"provider {provider.name!r} truncated a single-video annotation "
                        f"response at max_tokens={provider.max_tokens}; increase "
                        f"providers.{provider.name}.max_tokens or reduce the requested output"
                    )
                    if progress is not None:
                        progress(
                            ProgressEvent(
                                phase=phase,
                                kind="message",
                                completed=completed_videos,
                                total=len(videos),
                                unit="video",
                                detail=f"{label} response failed validation: {error}",
                            )
                        )
                    raise error

                video = batch[0]
                fallback = SemanticAnnotation(
                    video_id=video.video_id,
                    primary_category="Unclear",
                    subject=(
                        "Provider content filter prevented semantic annotation; "
                        "manual review required"
                    ),
                    tags=("content-filtered",),
                    content_type="unclassified",
                    confidence=0.0,
                )
                if progress is not None:
                    progress(
                        ProgressEvent(
                            phase=phase,
                            kind="message",
                            completed=completed_videos,
                            total=len(videos),
                            unit="video",
                            detail=(
                                f"{label} hit provider content filter for {video.video_id}; "
                                "recording Unclear fallback for manual review"
                            ),
                        )
                    )
                return AnnotationBatchResult(
                    annotations=(fallback,),
                    input_sha256=evidence_hash(batch),
                    usage=response.usage,
                    response_model=response.model,
                )

            split_at = (len(batch) + 1) // 2
            left_batch = batch[:split_at]
            right_batch = batch[split_at:]
            limit_reason = (
                "provider output limit"
                if response.finish_reason == "length"
                else "provider content filter"
            )
            if progress is not None:
                progress(
                    ProgressEvent(
                        phase=phase,
                        kind="message",
                        completed=completed_videos,
                        total=len(videos),
                        unit="video",
                        detail=(
                            f"{label} hit {limit_reason}; retrying {len(batch)} "
                            f"videos as {len(left_batch)} + {len(right_batch)}"
                        ),
                    )
                )

            left_response = _request_annotation_batch(
                provider, prompt, left_batch, progress, phase
            )
            note_response(f"{label}a")
            left = validate_batch(f"{label}a", left_batch, left_response)

            right_response = _request_annotation_batch(
                provider, prompt, right_batch, progress, phase
            )
            note_response(f"{label}b")
            right = validate_batch(f"{label}b", right_batch, right_response)

            return AnnotationBatchResult(
                annotations=left.annotations + right.annotations,
                input_sha256=evidence_hash(batch),
                usage=merge_usage(response.usage, left.usage, right.usage),
                response_model=(
                    left.response_model
                    if left.response_model == right.response_model
                    else response.model or left.response_model or right.response_model
                ),
            )

        try:
            value = parse_json_content(response, provider.name)
            annotations = validate_annotation_response(
                value,
                expected_video_ids=[video.video_id for video in batch],
                categories=set(prompt.categories),
            )
        except (ValueError, RuntimeError) as exc:
            if progress is not None:
                progress(
                    ProgressEvent(
                        phase=phase,
                        kind="message",
                        completed=completed_videos,
                        total=len(videos),
                        unit="video",
                        detail=f"{label} response failed validation: {exc}",
                    )
                )
            raise

        return AnnotationBatchResult(
            annotations=tuple(annotations),
            input_sha256=evidence_hash(batch),
            usage=response.usage,
            response_model=response.model,
        )

    initial_completed_batches = completed_batches

    def consume(
        task_index: int,
        item: tuple[int, list[ClassificationEvidence]],
        response: ChatResponse,
    ) -> AnnotationBatchResult:
        nonlocal completed_videos, completed_batches
        original_index, batch = item
        result = validate_batch(f"batch {original_index + 1}", batch, response)

        # Persist/apply the validated annotation batch before reporting it as complete.
        if on_batch is not None:
            on_batch(original_index, batch, result)

        completed_videos += len(result.annotations)
        completed_batches += 1
        if progress is not None:
            newly_completed = completed_batches - initial_completed_batches
            pending_remaining = len(pending) - newly_completed
            in_flight = min(provider.concurrency, pending_remaining)
            queued = max(0, pending_remaining - in_flight)
            progress(
                ProgressEvent(
                    phase=phase,
                    kind="update",
                    completed=completed_videos,
                    total=len(videos),
                    unit="video",
                    detail=(
                        f"{completed_batches}/{len(batches)} batches validated; "
                        f"{in_flight} in flight; {queued} queued"
                    ),
                    counters={"responses": responses_received},
                )
            )
        return result

    results = run_bounded_parallel(
        pending,
        concurrency=provider.concurrency,
        request=request,
        consume=consume,
        on_response=on_response,
    )

    ordered_batches = tuple(results[index] for index in range(len(pending)))
    annotations = tuple(
        annotation for batch in ordered_batches for annotation in batch.annotations
    )
    if progress is not None:
        progress(
            ProgressEvent(
                phase=phase,
                kind="finish",
                completed=completed_videos,
                total=len(videos),
                unit="video",
                detail=f"{len(batches)} batch(es) complete",
                counters={"responses": responses_received},
            )
        )
    return AnnotationRunResult(annotations=annotations, batches=ordered_batches)


def even_sample(
    videos: list[ClassificationEvidence],
    limit: int,
) -> list[ClassificationEvidence]:
    if limit < 1:
        raise ValueError("--taxonomy-sample must be at least 1")
    if len(videos) <= limit:
        return list(videos)
    if limit == 1:
        return [videos[len(videos) // 2]]
    indexes = {
        round(index * (len(videos) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [videos[index] for index in sorted(indexes)]


def _taxonomy_payload(videos: list[ClassificationEvidence]) -> list[dict[str, Any]]:
    return [
        {
            "video_id": video.video_id,
            "title": video.recovered_title or video.dearrow_title or video.original_title,
            "channel": video.channel,
        }
        for video in videos
    ]


def discover_taxonomy(
    provider: ProviderConfig,
    videos: list[ClassificationEvidence],
    *,
    interest_brief: str = "",
    max_categories: int = 20,
    progress: ProgressCallback | None = None,
    phase: str = "LLM taxonomy discovery",
) -> TaxonomyDiscovery:
    if not videos:
        raise ValueError("cannot discover a taxonomy from an empty video set")
    if not 3 <= max_categories <= 30:
        raise ValueError("--max-categories must be between 3 and 30")

    if progress is not None:
        progress(
            ProgressEvent(
                phase=phase,
                kind="start",
                completed=0,
                total=1,
                unit="request",
                detail=f"{len(videos)} sampled video(s)",
            )
        )

    prompt_hash = _canonical_hash(
        {
            "prompt_version": 1,
            "system": TAXONOMY_SYSTEM_PROMPT,
            "interest": interest_brief,
            "max_categories": max_categories,
            "schema": TAXONOMY_SCHEMA,
        }
    )
    payload = _taxonomy_payload(videos)
    input_sha = _canonical_hash(payload)
    response = chat(
        provider,
        [
            {"role": "system", "content": TAXONOMY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Create no more than {max_categories} broad categories.\n\n"
                    "## User interest context\n"
                    + (interest_brief or "No interest profile supplied.")
                    + "\n\n## Catalogue sample\n"
                    + json.dumps({"videos": payload}, ensure_ascii=False, sort_keys=True)
                    + "\n\n## Required JSON schema\n"
                    + json.dumps(TAXONOMY_SCHEMA, ensure_ascii=False, sort_keys=True)
                ),
            },
        ],
        json_schema=TAXONOMY_SCHEMA,
        **({"progress": progress, "phase": phase} if progress is not None else {}),
    )
    value = parse_json_content(response, provider.name)
    if set(value) != {"categories"} or not isinstance(value.get("categories"), list):
        raise ValueError("taxonomy response must contain only a categories array")

    categories: dict[str, str] = {}
    seen: set[str] = set()
    for index, row in enumerate(value["categories"]):
        if not isinstance(row, dict) or set(row) != {"name", "description"}:
            raise ValueError(f"taxonomy category[{index}] has missing or unexpected fields")
        name = _string(row["name"], f"categories[{index}].name")
        description = _string(row["description"], f"categories[{index}].description")
        folded = name.casefold()
        if folded in {name.casefold() for name in RESERVED_CATEGORIES}:
            continue
        if folded in seen:
            raise ValueError(f"taxonomy returned duplicate category {name!r}")
        seen.add(folded)
        categories[name] = description

    if len(categories) < 3:
        raise ValueError("taxonomy discovery returned fewer than 3 usable categories")
    if len(categories) > max_categories:
        raise ValueError(
            f"taxonomy discovery returned {len(categories)} categories; maximum is {max_categories}"
        )
    if progress is not None:
        progress(
            ProgressEvent(
                phase=phase,
                kind="finish",
                completed=1,
                total=1,
                unit="request",
                detail=f"{len(categories)} category/categories discovered",
            )
        )
    return TaxonomyDiscovery(
        categories=with_reserved_categories(categories),
        input_sha256=input_sha,
        prompt_sha256=prompt_hash,
        usage=response.usage,
        response_model=response.model,
    )
