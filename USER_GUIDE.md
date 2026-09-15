# youtube-watchlater-tidy user guide

This is the end-to-end operating guide for `youtube-watchlater-tidy`. It describes the **implemented workflow** rather than the development history.

The tool is deliberately conservative. The current normal workflow analyses and plans changes locally; it does **not** yet remove videos from Watch Later or execute playlist moves on YouTube. Local `delete`, `archive` and `move` actions are plans for a later execution stage.

Detailed implementation/reference notes live in [`docs/`](docs/).

## 1. How the workflow fits together

```text
Watch Later export
      |
      v
immutable SQLite snapshot
      |
      +--> repair missing live metadata
      +--> recover private/deleted metadata
      +--> creator / keyword / manual cohort triage
      +--> reusable rules
      +--> DeArrow alternate titles
      |
      v
LLM first pass over unresolved remainder
      |
      +--> confident suggestion ----------------------+
      |                                               |
      +--> needs_description                          |
                |                                     |
                v                                     |
        selective yt-dlp metadata                     |
                |                                     |
                v                                     |
        description refinement                        |
                |                                     |
                +--> still needs_transcript           |
                          |                            |
                          v                            |
                  transcript stage (planned)           |
                                                       v
                                             human review / execution
```

The important safety rules are:

1. **Imported snapshots are evidence.** Enrichment never rewrites the imported title/channel/position.
2. **Human and saved-rule decisions outrank LLM suggestions.** LLM output is advisory evidence stored separately from `decision_events`.
3. **Actions are local plans.** `delete`, `archive` and `move` currently do not mutate YouTube.
4. **Network work is selective and cached.** Use narrow targets and `--dry-run` where available.
5. **History is retained.** Decisions, metadata observations, archive lookups and LLM runs are append-only where practical.

Keep backups of both `watch-later.json` and `watchlater.sqlite3`.

## 2. Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Installed commands:

- `watchlater` — import, reports, selections, rules and archive recovery.
- `watchlater-dearrow` — DeArrow alternate-title enrichment.
- `watchlater-llm` — provider setup, classification, refinement and stored LLM runs.
- `watchlater-metadata` — selective full yt-dlp metadata enrichment without media download.

The default catalogue is `watchlater.sqlite3`. For another file, put `--db PATH` before the subcommand:

```bash
watchlater --db ~/private/watchlater.sqlite3 creators --remaining
watchlater-llm --db ~/private/watchlater.sqlite3 --config watchlater.toml results
```

## 3. Export Watch Later

Use a logged-in browser profile:

```bash
yt-dlp \
    --cookies-from-browser firefox \
    --flat-playlist \
    --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' \
    > watch-later.json
```

Change `firefox` if necessary.

The exact export order is stored as the snapshot position. Do **not** assume position means oldest/newest unless you deliberately established the YouTube playlist sort before exporting.

`--flat-playlist` is intentionally fast and can omit metadata. Later enrichment fills gaps without changing the source snapshot.

## 4. Import the snapshot

```bash
watchlater import watch-later.json
watchlater snapshots
```

The importer hashes the source file, so importing the exact same export again is idempotent. A later export becomes a new snapshot rather than overwriting the old one.

## 5. Initial inspection and metadata repair

Start with creators and unusual entries:

```bash
watchlater creators --remaining
watchlater videos --unknown-creator --remaining
watchlater videos --unavailable
```

### Repair live videos with missing creator metadata

```bash
watchlater enrich --missing-creator --dry-run
watchlater enrich --missing-creator
```

This runs full yt-dlp metadata extraction only for those live entries. The result is stored as a separate metadata observation.

Target one video explicitly:

```bash
watchlater enrich --video-id VIDEO_ID
watchlater enrich --video-id VIDEO_ID --refresh
```

## 6. Recover private/deleted videos

Watch Later can retain the YouTube ID after the video disappears. Recovery uses FindYouTubeVideo discovery and then tries metadata sources in this order:

1. Filmot metadata already present in the FindYouTubeVideo response.
2. PreserveTube metadata when discovery says a copy exists and Filmot gave no metadata.
3. A specific Wayback watch-page capture discovered by FindYouTubeVideo.

Run in batches:

```bash
watchlater recover --unavailable --limit 10
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

A normal incremental run skips cached found/not-found IDs **before** applying `--limit`, so repeating the command advances to the next uncached batch.

Inspect one result:

```bash
watchlater recovery VIDEO_ID
watchlater recovery VIDEO_ID --raw
```

### Refresh archive results

Prefer an explicit cohort when refreshing:

```bash
watchlater recover --video-id ID1 --video-id ID2 --refresh
watchlater recover --selection 12 --refresh

watchlater recover \
    --unavailable \
    --min-position 4908 \
    --max-position 4920 \
    --refresh
```

A broad `--unavailable --refresh --limit 10` deliberately revisits the first ten videos in that selected target.

## 7. Cheap/manual triage before LLMs

The goal is to remove obvious cohorts from the unresolved set cheaply.

### Creator cohorts

```bash
watchlater creators --remaining
watchlater select creator CHANNEL_ID --remaining
watchlater selection show
```

Prefer the stable channel ID from the report over a display name.

### Title cohorts

```bash
watchlater select title --contains 'Super Mario' --remaining

watchlater select title \
    --regex 'conference|keynote' \
    --remaining \
    --max-duration 2h
```

Selections can also use `--min-duration`, `--max-duration`, `--min-position` and `--max-position`.

### Keyword/phrase discovery

```bash
watchlater keywords --remaining --ngram 1 --min-count 3
watchlater keywords --remaining --ngram 2 --min-count 3
watchlater keywords --remaining --ngram 3 --min-count 3
```

The default report filters common/function-word phrases and series boilerplate while retaining technical tokens such as `C++`, `V.34` and `Z80`.

### Record local actions

The local actions are:

- `keep` — leave in Watch Later.
- `review` — unresolved; do not execute anything.
- `archive` — worthwhile reference, but eventually remove from Watch Later.
- `delete` — low-value/discard candidate; eventually remove from Watch Later.
- `move` — eventually add to a destination playlist first, then remove from Watch Later.

Examples:

```bash
watchlater selection action move --playlist 'Queue - Electronics'
watchlater selection action archive
watchlater selection action delete --reason 'stale event/news item'
```

These commands only change the local catalogue.

Undo while retaining history:

```bash
watchlater selection undo
```

Then recompute the remainder:

```bash
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3
```

### Export a selection

```bash
watchlater selection export --format json --output cohort.json
watchlater selection export --format csv --output cohort.csv
```

## 8. Reusable rules

Save a useful selection rule for future snapshots:

```bash
watchlater select creator CHANNEL_ID --remaining
watchlater selection save-rule \
    'electronics creator' \
    move \
    --playlist 'Queue - Electronics' \
    --priority 20
```

Inspect/apply:

```bash
watchlater rules list
watchlater rules apply --dry-run
watchlater rules apply
```

Rules only act on unresolved videos. Existing human decisions are never overwritten. Lower numeric priority runs first, then rule ID order.

Toggle rules with:

```bash
watchlater rules disable 3
watchlater rules enable 3
```

## 9. DeArrow alternate titles

DeArrow supplies alternate title evidence without replacing the imported YouTube title.

```bash
watchlater-dearrow enrich --all --remaining
```

Other targets:

```bash
watchlater-dearrow enrich --selection 12
watchlater-dearrow enrich --video-id VIDEO_ID
```

Inspect one result:

```bash
watchlater-dearrow show VIDEO_ID
watchlater-dearrow show VIDEO_ID --raw
```

Cache controls:

```bash
watchlater-dearrow enrich --all --max-age 7d
watchlater-dearrow enrich --video-id VIDEO_ID --refresh
```

Privacy-preserving hash-prefix lookup:

```bash
watchlater-dearrow enrich --all --remaining --hash-prefix
```

Only a trusted first submission (`locked` or non-negative votes) becomes the preferred **alternate** title. `original=true` means the original YouTube title remains preferred.

See [`docs/dearrow.md`](docs/dearrow.md).

## 10. Configure an LLM provider

Copy the examples:

```bash
cp examples/watchlater.example.toml watchlater.toml
cp examples/interests.example.md interests.md
```

The interest file is plain Markdown/prose describing what you find useful. The application supplies its own fixed task/schema instructions.

### Ollama

```toml
[providers.ollama-local]
preset = 'ollama'
model = 'qwen3:14b'
structured_mode = 'json_object'
```

Default endpoint: `http://127.0.0.1:11434/v1`.

### Unsloth models

Unsloth is treated as the model/export workflow, not a unique wire protocol. Serve an exported model through an OpenAI-compatible engine such as vLLM, llama-server or Ollama.

```toml
[providers.unsloth-local]
preset = 'unsloth'
model = 'my-org/my-unsloth-model'
structured_mode = 'json_schema'
```

The convenience preset defaults to `http://127.0.0.1:8000/v1`; override `base_url` for another serving engine.

### OpenRouter

```toml
[providers.openrouter]
preset = 'openrouter'
model = 'openai/gpt-5.4'

[providers.openrouter.headers]
HTTP-Referer = 'https://github.com/philpem/youtube-watchlater-tidy'
X-Title = 'youtube-watchlater-tidy'
```

Set the key outside the config:

```bash
export OPENROUTER_API_KEY='...'
```

The preset uses `https://openrouter.ai/api/v1` and `OPENROUTER_API_KEY` by default.

### Generic OpenAI-compatible endpoint

```toml
[providers.remote]
preset = 'generic'
base_url = 'https://llm.example.invalid/v1'
model = 'example-model'
api_key_env = 'WATCHLATER_LLM_API_KEY'
```

Literal API keys/secrets in TOML are rejected.

### Verify provider and prompt

```bash
watchlater-llm --config watchlater.toml providers
watchlater-llm --config watchlater.toml probe --provider ollama-local
watchlater-llm --config watchlater.toml prompt
watchlater-llm --config watchlater.toml prompt --hash-only
```

See [`docs/llm.md`](docs/llm.md).

## 11. First-pass LLM classification

Start with a small dry run:

```bash
watchlater-llm \
    --config watchlater.toml \
    classify \
    --provider ollama-local \
    --limit 20 \
    --dry-run
```

Then classify:

```bash
watchlater-llm \
    --config watchlater.toml \
    classify \
    --provider ollama-local \
    --limit 20 \
    --batch-size 5
```

Or select another configured provider, for example OpenRouter:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider openrouter --limit 20 --batch-size 5
```

The classifier only considers unresolved videos and returns validated suggestions including action, topic/content type, timeliness, quality/confidence, destination proposal, `needs_description` and `needs_transcript`.

### Cache and history

Normal classification uses an exact-run cache keyed by output-affecting provider settings, prompt hash and the whole ordered evidence target. API-key values are not included.

Force a new append-only run:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --refresh
```

Run without cache/storage:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --no-store
```

Inspect stored results:

```bash
watchlater-llm --config watchlater.toml results
watchlater-llm --config watchlater.toml results --run-id 12
```

A later human/rule decision does not rewrite old LLM evidence; it simply takes precedence.

## 12. Description escalation and second-pass refinement

A first-pass LLM run may say `needs_description=true`. Do not fetch descriptions for the whole catalogue.

### Fetch only the requested descriptions

Latest stored LLM run:

```bash
watchlater-metadata enrich --llm-needs-description --dry-run
watchlater-metadata enrich --llm-needs-description
```

Specific parent run:

```bash
watchlater-metadata enrich --llm-needs-description --run-id 12
```

Other metadata targets remain available:

```bash
watchlater-metadata enrich --missing-description
watchlater-metadata enrich --selection 12
watchlater-metadata enrich --video-id ID1 --video-id ID2
```

The default metadata fetch uses four workers and starts requests at least 0.5 seconds apart. Tune conservatively if YouTube throttles:

```bash
watchlater-metadata enrich --llm-needs-description \
    --workers 2 --interval 1.0
```

No video/audio media is downloaded. Full yt-dlp JSON is retained; common fields are normalized into `metadata_observations`.

Inspect one observation:

```bash
watchlater-metadata show VIDEO_ID
watchlater-metadata show VIDEO_ID --raw
```

### Reclassify the exact parent run with descriptions

Inspect what will be sent:

```bash
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 \
    --provider ollama-local \
    --dry-run
```

Then run it:

```bash
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 \
    --provider ollama-local
```

The refinement includes only videos from parent run 12 that:

- had `needs_description=true`;
- are still unresolved by a human/rule decision; and
- now have a non-empty description.

Videos still missing descriptions are reported and skipped. Fetch them with `watchlater-metadata` before retrying.

Each refinement input includes the original cheap evidence, previous validated LLM suggestion, new description and description provenance. Descriptions are capped at 4000 characters by default:

```bash
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --max-description-chars 8000
```

The second pass uses a distinct refinement prompt and tells the model to reconsider rather than merely repeat the old answer. It should clear `needs_description` when the description resolves the ambiguity and only request `needs_transcript` if spoken content remains materially necessary.

The child result is another append-only LLM run. `watchlater-llm results --run-id CHILD_ID` shows its context, including `stage=description_refinement` and `parent_run_id=12`. The original run remains unchanged.

The same cache controls apply:

```bash
watchlater-llm --config watchlater.toml refine-description --run-id 12 --refresh
watchlater-llm --config watchlater.toml refine-description --run-id 12 --no-store
```

See [`docs/llm-classification.md`](docs/llm-classification.md) and [`docs/rich-metadata.md`](docs/rich-metadata.md).

## 13. Recommended end-to-end run

```bash
# 1. Export and import
yt-dlp --cookies-from-browser firefox --flat-playlist --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' > watch-later.json
watchlater import watch-later.json

# 2. Repair/recover obvious metadata gaps
watchlater enrich --missing-creator
watchlater recover --unavailable --limit 20

# 3. Cheap manual/rule triage
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3
# ... select, inspect, action and save rules ...

# 4. Cheap alternate-title evidence
watchlater-dearrow enrich --all --remaining

# 5. First LLM pass
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --dry-run
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5

# 6. Suppose that stored first pass was run 12. Fetch only requested descriptions
watchlater-metadata enrich --llm-needs-description --run-id 12

# 7. Refine run 12 with descriptions
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local --dry-run
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local

# 8. Inspect child/parent results and continue human decisions
watchlater-llm --config watchlater.toml results
watchlater creators --remaining
```

The next escalation stage is transcripts/captions, only for the smaller subset that still returns `needs_transcript=true` after description refinement.

## 14. Cache/refresh cheat sheet

`refresh` means "perform a new operation despite a cache", after choosing the target.

- `watchlater recover` — normal incremental runs skip cached IDs before `--limit`; use explicit IDs/selection/position range with `--refresh` when possible.
- `watchlater-dearrow` — found/not-found results are cached; use `--max-age` for TTL or `--refresh` for the chosen target.
- `watchlater-metadata` — successful yt-dlp observations are cached; `--refresh` refetches the chosen cohort.
- `watchlater-llm classify` — exact provider+prompt+evidence runs are cached; `--refresh` stores another run; `--no-store` bypasses persistence.
- `watchlater-llm refine-description` — same semantics, but its distinct refinement prompt and description-bearing evidence give it a separate cache identity from the first pass.

Use `--dry-run` before large network/provider operations when available.

## 15. Current limitations

Project issues include planned features that may not yet exist. Currently:

- **Selective YouTube execution is not implemented in the normal workflow.** Local `move`, `archive` and `delete` decisions do not change YouTube.
- **Playlist synchronization/execution is not implemented.** A `move` destination is a plan, not proof that the destination playlist contains the video.
- **Transcript/caption escalation is not implemented yet.** `needs_transcript` is currently a request from the classifier/refinement stage.
- **The local HTML human-review report is not implemented yet.** Use CLI reports, selection exports and stored LLM results meanwhile.

The destructive "clear all Watch Later" browser-console snippet in the main README is separate from this selective workflow. Keep an export/backup before using it.

## 16. Detailed documentation

- [`docs/README.md`](docs/README.md) — documentation index.
- [`docs/dearrow.md`](docs/dearrow.md) — DeArrow API, trust rule, privacy and attribution.
- [`docs/llm.md`](docs/llm.md) — provider configuration, prompt profiles and OpenAI-compatible transport.
- [`docs/llm-classification.md`](docs/llm-classification.md) — classification/refinement evidence, validation, persistence and cache behavior.
- [`docs/rich-metadata.md`](docs/rich-metadata.md) — selective yt-dlp description/rich metadata enrichment.

Use `COMMAND --help` as the definitive option reference for the installed version.
