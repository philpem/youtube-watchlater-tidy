# LLM classification engine

The first classification engine deliberately prints suggestions without storing them. This
makes it possible to test providers, prompts, batch sizing and validation against the real
catalogue before LLM results become persistent evidence.

## Candidate precedence

Only videos without a current decision are sent to an LLM. A human decision or saved-rule
decision therefore prevents an LLM call for that video, even if the video is present in an
explicit saved selection.

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
- trusted DeArrow alternate title, when the optional DeArrow cache exists;
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

A normal classification run sends batches through the selected OpenAI-compatible provider
and then validates the returned JSON again in the application. Among other checks:

- every requested video ID must appear exactly once and no extra IDs are accepted;
- actions and timeliness values must be from the fixed enums;
- confidence/quality values must be numeric and in the 0..1 range;
- an `existing_playlist` must exactly match a configured controlled playlist name;
- a new playlist proposal must begin with `Queue - `;
- a `move` action must include exactly one existing/new destination;
- escalation flags must be booleans;
- missing or unexpected response fields are rejected.

Validated results are printed as JSON together with the prompt hash, per-batch evidence
hash, response model and usage information returned by the server:

```bash
watchlater-llm --config watchlater.toml --db watchlater.sqlite3 classify \
    --provider ollama-local --limit 20 --batch-size 5
```

This stage does **not** modify `decision_events` or create playlist operations. Persistent
LLM classification/cache tables are a separate schema migration after the DeArrow v6
migration is established on the main branch.
