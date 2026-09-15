# youtube-watchlater-tidy user guide

This guide describes the **current, implemented workflow** for importing, analysing, enriching and classifying a large YouTube Watch Later playlist.

The tool is intentionally conservative. Most commands only change the local SQLite catalogue. At the time of writing, the normal triage/classification workflow **does not remove videos from Watch Later or create/move videos between YouTube playlists**. Decisions such as `delete`, `archive` and `move` are local plans for a later execution stage.

For implementation details, see the focused documents in [`docs/`](docs/).

## 1. Mental model and safety rules

The workflow is designed as a funnel:

```text
Watch Later export
      |
      v
immutable snapshot catalogue
      |
      +--> creator / keyword / manual cohort rules
      |
      +--> archive recovery for deleted/private videos
      |
      +--> DeArrow alternate titles
      |
      +--> LLM first-pass classification of unresolved videos
      |         |
      |         +--> needs_description
      |                   |
      |                   +--> selective yt-dlp rich metadata
      |
      +--> human review / later execution
```

The main safety rules are:

1. **Imported snapshots are evidence.** Later enrichment does not rewrite the imported title/channel/etc.
2. **Human/rule decisions outrank LLM suggestions.** LLM output is stored separately and never becomes a `current_decision` automatically.
3. **Actions are local plans.** `delete`, `archive` and `move` do not currently mutate YouTube.
4. **Network enrichment is selective and cached.** Re-running without `--refresh` normally reuses or skips previously fetched evidence.
5. **History is append-only where practical.** Old decisions, metadata observations, archive lookups and LLM classification runs remain available for provenance.

Back up both the original `watch-later.json` export and `watchlater.sqlite3` before experimenting with destructive browser scripts or future execution features.

## 2. Install

Create a virtual environment and install the project in editable mode:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

The install provides these commands:

- `watchlater` — catalogue import, reports, cohort selection, rules and archive recovery.
- `watchlater-dearrow` — DeArrow alternate-title enrichment.
- `watchlater-llm` — LLM provider configuration, classification and stored LLM results.
- `watchlater-metadata` — selective full yt-dlp metadata enrichment without downloading media.

Most commands default to `watchlater.sqlite3`. For another catalogue, put `--db PATH` **before the subcommand**, for example:

```bash
watchlater --db ~/private/watchlater.sqlite3 creators --remaining
watchlater-llm --db ~/private/watchlater.sqlite3 --config watchlater.toml results
```

## 3. Export Watch Later

Use yt-dlp with a logged-in browser profile. The flat export is quick and does not download media:

```bash
yt-dlp \
    --cookies-from-browser firefox \
    --flat-playlist \
    --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' \
    > watch-later.json
```

Change `firefox` if necessary.

The exact order in the export is stored as the snapshot position. Do **not** assume that position means oldest/newest unless you deliberately established the YouTube playlist sort before exporting.

`--flat-playlist` can omit metadata for some entries. That is expected; enrichment commands can fill gaps later without changing the source snapshot.

## 4. Import the snapshot

```bash
watchlater import watch-later.json
watchlater snapshots
```

The importer hashes the source file and is idempotent: importing the exact same export again returns the existing snapshot rather than duplicating it.

Later exports become separate snapshots, so old metadata and positions remain available.

## 5. Inspect the catalogue before classifying

Start with creator groups:

```bash
watchlater creators --remaining
```

Useful individual-video reports include:

```bash
watchlater videos --remaining
watchlater videos --unknown-creator --remaining
watchlater videos --unavailable
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

`--remaining` means videos without a current human/rule decision.

### Missing creator metadata on live videos

For otherwise-live videos whose flat-playlist entry lacks creator information:

```bash
watchlater enrich --missing-creator --dry-run
watchlater enrich --missing-creator
```

This runs full yt-dlp metadata extraction for only those videos, using `--skip-download`. It stores a separate `yt-dlp` metadata observation and leaves the imported row unchanged.

Target one live video explicitly with:

```bash
watchlater enrich --video-id VIDEO_ID
watchlater enrich --video-id VIDEO_ID --refresh
```

## 6. Recover deleted/private videos

Watch Later often retains a video ID even after the video becomes private or deleted. The recovery command asks FindYouTubeVideo for archive/index evidence and then tries metadata sources in this order:

1. Filmot metadata already returned inside FindYouTubeVideo.
2. PreserveTube metadata, only when discovery says it has a copy and Filmot yielded no metadata.
3. A specific Wayback watch-page capture already discovered by FindYouTubeVideo.

Run incrementally:

```bash
watchlater recover --unavailable --limit 10
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

Cached found/not-found results are skipped on a normal run **before** `--limit` is applied, so repeating the command advances to the next uncached batch.

Inspect a cached lookup:

```bash
watchlater recovery VIDEO_ID
watchlater recovery VIDEO_ID --raw
```

### Refreshing archive lookups

`--refresh` means "ignore the archive cache for this explicitly selected target". Prefer a narrow target when refreshing:

```bash
watchlater recover --video-id ID1 --video-id ID2 --refresh

watchlater recover \
    --unavailable \
    --min-position 4908 \
    --max-position 4920 \
    --refresh

watchlater recover --selection 12 --refresh
```

A broad `--unavailable --refresh --limit 10` deliberately revisits the first ten videos in that target rather than advancing to the next cached page.

## 7. Cheap/manual triage first

The intended workflow is to remove obvious cohorts from the unresolved set before using an LLM.

### Creators

```bash
watchlater creators --remaining
watchlater select creator CHANNEL_ID --remaining
watchlater selection show
```

Prefer the stable channel ID from the creator report over a display name.

### Title matching

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

The default keyword report suppresses common/function-word phrases and series boilerplate while retaining technical tokens such as `C++`, `V.34`, `Z80` and similar terms.

### Record a local action

Supported actions are:

- `keep` — leave in Watch Later.
- `review` — unresolved / do not execute anything yet.
- `archive` — worthwhile reference, but eventually remove from Watch Later.
- `delete` — low-value/discard candidate; eventually remove from Watch Later.
- `move` — add to a destination playlist, then eventually remove from Watch Later.

Examples:

```bash
watchlater selection action move --playlist 'Queue - Electronics'
watchlater selection action archive
watchlater selection action delete --reason 'stale event/news item'
```

These commands update only the local catalogue.

Undo a cohort decision while retaining history:

```bash
watchlater selection undo
```

Then recalculate the remainder:

```bash
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3
```

### Export a selection

```bash
watchlater selection export --format json --output cohort.json
watchlater selection export --format csv --output cohort.csv
```

The export includes selection provenance and current decisions, not just video IDs.

## 8. Reusable saved rules

Once a cohort rule is clearly useful, save its selector for later snapshots.

For example:

```bash
watchlater select creator CHANNEL_ID --remaining

watchlater selection save-rule \
    'electronics creator' \
    move \
    --playlist 'Queue - Electronics' \
    --priority 20
```

Inspect and apply rules:

```bash
watchlater rules list
watchlater rules apply --dry-run
watchlater rules apply
```

Rules run against unresolved videos. Existing human decisions are not overwritten. When several rules overlap, lower numeric priority runs first, then rule ID order.

Rules can be toggled:

```bash
watchlater rules disable 3
watchlater rules enable 3
```

## 9. Add DeArrow alternate titles

DeArrow can provide less-clickbait alternate titles. The original YouTube title remains separate.

For unresolved videos:

```bash
watchlater-dearrow enrich --all --remaining
```

Other targets:

```bash
watchlater-dearrow enrich --selection 12
watchlater-dearrow enrich --video-id VIDEO_ID
```

Inspect one cached result:

```bash
watchlater-dearrow show VIDEO_ID
watchlater-dearrow show VIDEO_ID --raw
```

By default found/not-found results are cached. Use a TTL when you want stale lookups refreshed:

```bash
watchlater-dearrow enrich --all --max-age 7d
```

Or explicitly refresh a narrow target:

```bash
watchlater-dearrow enrich --video-id VIDEO_ID --refresh
```

For privacy-preserving lookup, use the DeArrow SHA-256 prefix API:

```bash
watchlater-dearrow enrich --all --remaining --hash-prefix
```

Only a trusted first DeArrow submission (`locked` or non-negative votes) becomes the preferred **alternate** title. A submission marked `original=true` means the original YouTube title remains preferred.

See [`docs/dearrow.md`](docs/dearrow.md) for provenance and attribution details.

## 10. Configure the LLM layer

LLM classification is intended for the awkward remainder after manual/rule triage and cheap enrichment.

Copy the examples:

```bash
cp examples/watchlater.example.toml watchlater.toml
cp examples/interests.example.md interests.md
```

The interest file is plain prose/Markdown. It should describe what you care about; it does not need JSON/schema instructions.

### Ollama

```toml
[providers.ollama-local]
preset = 'ollama'
model = 'qwen3:14b'
structured_mode = 'json_object'
```

Default endpoint: `http://127.0.0.1:11434/v1`.

### Unsloth models

Unsloth is a model/training/export workflow rather than a distinct classification wire protocol here. Serve the exported model through an OpenAI-compatible engine such as vLLM, llama-server or Ollama.

The convenience preset defaults to vLLM:

```toml
[providers.unsloth-local]
preset = 'unsloth'
model = 'my-org/my-unsloth-model'
structured_mode = 'json_schema'
```

Default endpoint: `http://127.0.0.1:8000/v1`. Override `base_url` when serving it elsewhere.

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

The OpenRouter preset defaults to `https://openrouter.ai/api/v1` and the `OPENROUTER_API_KEY` environment variable.

### Generic OpenAI-compatible endpoint

```toml
[providers.remote]
preset = 'generic'
base_url = 'https://llm.example.invalid/v1'
model = 'example-model'
api_key_env = 'WATCHLATER_LLM_API_KEY'
```

Literal API keys/secrets in the TOML are rejected. Only the environment-variable name is stored.

### Check the provider and prompt

```bash
watchlater-llm --config watchlater.toml providers
watchlater-llm --config watchlater.toml probe --provider ollama-local
watchlater-llm --config watchlater.toml prompt
watchlater-llm --config watchlater.toml prompt --hash-only
```

The prompt hash covers the fixed application instructions, classification schema, interest profile and controlled playlist guidance.

See [`docs/llm.md`](docs/llm.md) for provider/config details.

## 11. First-pass LLM classification

Always start with a dry run on a small target so you can see exactly what evidence will be sent:

```bash
watchlater-llm \
    --config watchlater.toml \
    classify \
    --provider ollama-local \
    --limit 20 \
    --dry-run
```

Then run classification:

```bash
watchlater-llm \
    --config watchlater.toml \
    classify \
    --provider ollama-local \
    --limit 20 \
    --batch-size 5
```

Or use OpenRouter:

```bash
watchlater-llm \
    --config watchlater.toml \
    classify \
    --provider openrouter \
    --limit 20 \
    --batch-size 5
```

The classifier only considers unresolved videos. It returns validated suggestions including:

- action (`keep`, `review`, `archive`, `delete`, `move`);
- topic/content type;
- timeliness and quality;
- confidence/reason;
- existing/new queue destination proposals;
- `needs_description`;
- `needs_transcript`.

### LLM cache and history

Normal classification uses an exact-run cache. A cache hit requires the same output-affecting provider configuration, prompt hash and ordered evidence target. API-key values are not part of the fingerprint.

Force a new append-only run:

```bash
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --refresh
```

Run without storing anything:

```bash
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --no-store
```

Inspect stored results:

```bash
watchlater-llm --config watchlater.toml results
watchlater-llm --config watchlater.toml results --run-id 12
```

If a human/rule decision is made after the LLM run, the stored LLM suggestion remains historical evidence and the results display can show the newer current decision alongside it.

See [`docs/llm-classification.md`](docs/llm-classification.md).

## 12. Escalate only ambiguous videos to descriptions

Do not fetch full metadata for the whole catalogue just because a few videos are ambiguous.

Target the latest stored LLM run's `needs_description=true` videos:

```bash
watchlater-metadata enrich --llm-needs-description --dry-run
watchlater-metadata enrich --llm-needs-description
```

Or specify a historical run:

```bash
watchlater-metadata enrich --llm-needs-description --run-id 12
```

Other useful targets are:

```bash
watchlater-metadata enrich --missing-description
watchlater-metadata enrich --selection 12
watchlater-metadata enrich --video-id ID1 --video-id ID2
```

The default uses four workers and starts requests at least 0.5 seconds apart. Tune conservatively if YouTube starts throttling:

```bash
watchlater-metadata enrich \
    --llm-needs-description \
    --workers 2 \
    --interval 1.0
```

No video/audio media is downloaded. Full yt-dlp JSON is retained as evidence, while common fields such as description, upload date, availability, title/channel, duration and view count are normalized into `metadata_observations`.

Inspect one observation:

```bash
watchlater-metadata show VIDEO_ID
watchlater-metadata show VIDEO_ID --raw
```

See [`docs/rich-metadata.md`](docs/rich-metadata.md).

## 13. Recommended end-to-end run

A practical first pass over a large catalogue looks like this:

```bash
# 1. Export and import
yt-dlp --cookies-from-browser firefox --flat-playlist --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' > watch-later.json
watchlater import watch-later.json

# 2. Repair/recover obvious metadata gaps
watchlater enrich --missing-creator
watchlater recover --unavailable --limit 20

# 3. Manually remove obvious creator/title cohorts from the unresolved set
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3

# ... use `select`, inspect, then action/save rules ...

# 4. Add cheap semantic evidence
watchlater-dearrow enrich --all --remaining

# 5. Inspect and run a small LLM pass
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --dry-run
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5

# 6. Fetch descriptions only where requested
watchlater-metadata enrich --llm-needs-description

# 7. Inspect stored suggestions / continue manual decisions
watchlater-llm --config watchlater.toml results
watchlater creators --remaining
```

The next refinement stage is description-aware reclassification, followed by transcript/caption escalation for the smaller subset that still needs it.

## 14. Cache/refresh cheat sheet

The word `refresh` always means "perform a new network/provider operation despite a cache", but the **target is selected first**.

- `watchlater recover`: cached archive results are skipped on ordinary incremental runs; use explicit IDs/selection/position ranges with `--refresh` when possible.
- `watchlater-dearrow`: found/not-found lookups are cached; use `--max-age` for TTL refresh or `--refresh` for an explicit target.
- `watchlater-metadata`: successful yt-dlp metadata observations are cached; `--refresh` refetches the chosen cohort.
- `watchlater-llm classify`: exact provider+prompt+evidence runs are cached; `--refresh` stores another historical run; `--no-store` bypasses persistence.

When in doubt, add `--dry-run` where available before a potentially large network operation.

## 15. Current limitations

These are important because project issues describe future work that may not be implemented yet.

- **Selective YouTube execution is not yet part of the normal workflow.** Local `move`, `archive` and `delete` decisions do not currently change YouTube.
- **Playlist synchronization/execution is not yet implemented.** A `move` destination is a plan, not proof the video is present in that playlist.
- **Description-aware second-pass LLM reclassification is the next refinement stage.** Description fetching exists now; automatic second-pass use is not yet documented as implemented.
- **Transcript/caption escalation is not yet implemented.** `needs_transcript` is currently a request/proposal from the classifier.
- **The local HTML human-review report is not yet implemented.** Use CLI reports/exports and stored LLM results meanwhile.

The deliberately destructive "clear all Watch Later" browser-console snippet in the main README is separate from this selective workflow and should only be used after keeping an export/backup.

## 16. Detailed documentation

- [`docs/dearrow.md`](docs/dearrow.md) — DeArrow API, trust rule, privacy and attribution.
- [`docs/llm.md`](docs/llm.md) — provider configuration, prompt profiles, OpenAI-compatible transport.
- [`docs/llm-classification.md`](docs/llm-classification.md) — classification evidence, validation, persistence and cache behavior.
- [`docs/rich-metadata.md`](docs/rich-metadata.md) — selective yt-dlp description/rich metadata enrichment.

Use `COMMAND --help` as the definitive option reference for the installed version.