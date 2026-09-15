# LLM classification engine

LLM classification is a late-stage advisory layer. It operates only after cheap/manual
triage has reduced the unresolved set, validates every provider response in the
application, and stores successful suggestions as append-only evidence rather than as
catalogue decisions.

## Candidate precedence

Only videos without a current decision are sent to an LLM. A human decision or saved-rule
decision therefore prevents an LLM call for that video, even if the video is present in an
explicit saved selection or a previous LLM run requested more evidence.

By default the latest snapshot is used. A saved selection can restrict the unresolved
candidate set:

```bash
watchlater-llm --config watchlater.toml --db watchlater.sqlite3 classify --selection 12
```

Use `--limit` for a small experiment.

## Evidence

The cheap first-pass evidence contains:

- exact video ID and playlist position;
- immutable imported YouTube title;
- recovered historical title/source for unavailable videos, where available;
- trusted DeArrow alternate title;
- channel name/ID;
- duration and view count;
- enriched upload date and availability where available.

Descriptions and transcripts are intentionally omitted from this first pass. The model can
request escalation using `needs_description` and `needs_transcript` instead.

Inspect the exact evidence and hashes without contacting an LLM:

```bash
watchlater-llm --config watchlater.toml --db watchlater.sqlite3 classify \
    --provider ollama-local --limit 20 --dry-run
```

## Validation

A classification run sends batches through the selected OpenAI-compatible provider and
then validates the returned JSON again in the application. Among other checks:

- every requested video ID must appear exactly once and no extra IDs are accepted;
- actions and timeliness values must be from the fixed enums;
- confidence/quality values must be numeric and in the 0..1 range;
- an `existing_playlist` must exactly match a configured controlled playlist name;
- a new playlist proposal must begin with `Queue - `;
- a `move` action must include exactly one existing/new destination;
- escalation flags must be booleans;
- missing or unexpected response fields are rejected.

## Persistence and exact-run cache

Successful validated runs are stored in schema v7 as three append-only layers:

1. run provenance/cache identity (snapshot, provider fingerprint, prompt/input hashes);
2. provider batches (input hash, response model, usage and validated response JSON);
3. per-video evidence hashes/JSON and validated suggestions.

LLM rows are intentionally **not** written to `decision_events`. A later human or saved-rule
decision therefore outranks the stored suggestion automatically while the historical LLM
result remains inspectable.

The default `classify` behavior is:

```bash
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20
```

Before contacting the model it looks for a completed run with the same:

- snapshot/target evidence;
- provider fingerprint;
- effective prompt hash.

The provider fingerprint includes provider identity/base URL, model, temperature, output
token limit, structured-output mode and provider-specific request fields. It deliberately
does **not** include API-key values, retry count or concurrency.

The evidence hash covers the whole ordered target, including batch-relevant video evidence.
This deliberately avoids reusing an individual suggestion that was originally generated in
a different batch context.

On an exact cache hit the stored run is returned without a provider call. Use `--refresh`
to append a fresh historical run even when an exact cache exists:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider openrouter --limit 20 --refresh
```

Use `--no-store` for the previous ephemeral behavior: it neither checks nor writes the LLM
cache.

## Description refinement

A first-pass run may return `needs_description=true`. Fetch descriptions only for that
subset with the metadata command:

```bash
watchlater-metadata enrich --llm-needs-description --run-id 12
```

Then refine the same historical run:

```bash
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 \
    --provider ollama-local \
    --dry-run

watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 \
    --provider ollama-local
```

The refinement target is the parent run's `needs_description=true` videos that are still
unresolved **and** now have a non-empty description. Videos that gained a human/rule
decision after the parent run are skipped. Videos still missing a description are reported
and are not sent to the model.

Each refinement evidence item contains:

- the exact parent run ID;
- the previous cheap evidence;
- the previous validated classification;
- the newly available description and its source;
- whether the description was truncated.

Descriptions are capped at 4000 characters by default to control prompt size. Override the
cap when necessary:

```bash
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --max-description-chars 8000
```

The description stage has a distinct prompt hash. It explicitly asks the model to
reconsider the old suggestion rather than merely repeat it, to clear `needs_description`
when the description resolves the ambiguity, and to set `needs_transcript=true` only when
spoken content is still materially necessary.

A stored refinement is another append-only LLM run. Its `context` identifies
`stage=description_refinement`, `parent_run_id`, and the description cap. The original run
is retained unchanged. The normal exact-run cache, `--refresh`, and `--no-store` semantics
also apply to refinement runs.

## Inspecting stored results

Show the most recent stored run for the latest/specified snapshot, or an exact run ID:

```bash
watchlater-llm --config watchlater.toml results
watchlater-llm --config watchlater.toml results --snapshot 2
watchlater-llm --config watchlater.toml results --run-id 12
```

The output includes run `context`, each historical LLM suggestion, and any **current**
human/rule decision for the same video. The current decision is joined at read time; it
does not rewrite the stored classification.

Playlist creation, movement and Watch Later deletion remain separate execution stages.
