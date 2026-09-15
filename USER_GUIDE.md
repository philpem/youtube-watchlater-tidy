# youtube-watchlater-tidy user guide

This is the operating guide for the **implemented** `youtube-watchlater-tidy` workflow. Detailed reference notes live in [`docs/`](docs/).

The tool is deliberately conservative: the current normal workflow analyses and plans changes locally. It does **not** yet remove videos from Watch Later or execute playlist moves on YouTube. Local `delete`, `archive` and `move` actions are plans for a later execution stage.

## 1. Workflow and safety model

```text
Watch Later export
      |
      v
immutable SQLite snapshot
      |
      +--> repair/recover missing metadata
      +--> creator/keyword/manual cohorts + saved rules
      +--> DeArrow alternate titles
      |
      v
LLM first pass over unresolved remainder
      |
      +--> needs_description
      |        |
      |        +--> selective yt-dlp metadata
      |                 |
      |                 +--> description refinement
      |
      +--> needs_transcript
               |
               +--> selective manual/automatic captions
                        |
                        +--> transcript-aware LLM refinement (planned)

Human review / later execution is always authoritative.
```

Rules to keep in mind:

1. Imported snapshots are evidence; enrichment never rewrites them.
2. Human and saved-rule decisions outrank LLM suggestions.
3. LLM suggestions are append-only advisory evidence, not `decision_events`.
4. Network work is selective and cached; use `--dry-run` where available.
5. Missing descriptions/captions are not negative quality signals.

Keep backups of `watch-later.json` and `watchlater.sqlite3`.

## 2. Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Commands installed:

- `watchlater` — import, reports, selections, rules, archive recovery and basic metadata repair.
- `watchlater-dearrow` — DeArrow alternate-title enrichment.
- `watchlater-llm` — provider setup, classification/refinement and stored LLM runs.
- `watchlater-metadata` — selective full yt-dlp metadata/description enrichment.
- `watchlater-transcript` — selective subtitle/caption transcript acquisition.

All default to `watchlater.sqlite3`; put `--db PATH` before a subcommand when using another catalogue.

## 3. Export and import Watch Later

```bash
yt-dlp \
    --cookies-from-browser firefox \
    --flat-playlist \
    --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' \
    > watch-later.json

watchlater import watch-later.json
watchlater snapshots
```

The import is hashed/idempotent. Later exports become new snapshots. Snapshot position records the export order; do not assume it means age unless you deliberately controlled YouTube's sort order first.

## 4. Repair/recover missing metadata

Inspect obvious gaps:

```bash
watchlater creators --remaining
watchlater videos --unknown-creator --remaining
watchlater videos --unavailable
```

Repair live videos whose flat export lacks creator metadata:

```bash
watchlater enrich --missing-creator --dry-run
watchlater enrich --missing-creator
```

Recover deleted/private IDs from public archives:

```bash
watchlater recover --unavailable --limit 10
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

Recovery uses FindYouTubeVideo discovery, then Filmot, PreserveTube and Wayback metadata where available.

Normal recovery skips cached IDs before `--limit`, so repeated batches advance. Refresh a specific cohort when necessary:

```bash
watchlater recover --video-id ID1 --video-id ID2 --refresh
watchlater recover --selection 12 --refresh
watchlater recover --unavailable --min-position 4908 --max-position 4920 --refresh
```

Inspect one cached result:

```bash
watchlater recovery VIDEO_ID
watchlater recovery VIDEO_ID --raw
```

## 5. Cheap/manual triage

Do this before LLM work.

```bash
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3
```

Select cohorts:

```bash
watchlater select creator CHANNEL_ID --remaining
watchlater select title --contains 'Super Mario' --remaining
watchlater select title --regex 'conference|keynote' --remaining --max-duration 2h
watchlater selection show
```

Record a local plan:

```bash
watchlater selection action keep
watchlater selection action review
watchlater selection action archive
watchlater selection action delete --reason 'stale event/news item'
watchlater selection action move --playlist 'Queue - Electronics'
```

These commands do not change YouTube. Undo while retaining history with:

```bash
watchlater selection undo
```

Export a cohort:

```bash
watchlater selection export --format json --output cohort.json
watchlater selection export --format csv --output cohort.csv
```

## 6. Reusable rules

```bash
watchlater select creator CHANNEL_ID --remaining
watchlater selection save-rule \
    'electronics creator' move \
    --playlist 'Queue - Electronics' --priority 20

watchlater rules list
watchlater rules apply --dry-run
watchlater rules apply
```

Rules only claim unresolved videos. Existing human decisions are never overwritten. Lower numeric priority runs first.

## 7. DeArrow titles

```bash
watchlater-dearrow enrich --all --remaining
watchlater-dearrow enrich --selection 12
watchlater-dearrow show VIDEO_ID
```

Cache/refresh examples:

```bash
watchlater-dearrow enrich --all --max-age 7d
watchlater-dearrow enrich --video-id VIDEO_ID --refresh
watchlater-dearrow enrich --all --remaining --hash-prefix
```

The imported YouTube title remains separate. Only a trusted first DeArrow submission (`locked` or non-negative votes) becomes the preferred **alternate** title; `original=true` means keep the YouTube title.

See [`docs/dearrow.md`](docs/dearrow.md).

## 8. Configure LLM providers

Copy the examples:

```bash
cp examples/watchlater.example.toml watchlater.toml
cp examples/interests.example.md interests.md
```

The interest file is plain Markdown/prose. The application supplies the fixed task and output schema.

### Ollama

```toml
[providers.ollama-local]
preset = 'ollama'
model = 'qwen3:14b'
structured_mode = 'json_object'
```

Default endpoint: `http://127.0.0.1:11434/v1`.

### Unsloth models

Serve an Unsloth-trained/exported model through an OpenAI-compatible runtime such as vLLM, llama-server or Ollama. The convenience preset defaults to vLLM:

```toml
[providers.unsloth-local]
preset = 'unsloth'
model = 'my-org/my-unsloth-model'
structured_mode = 'json_schema'
```

### OpenRouter

```toml
[providers.openrouter]
preset = 'openrouter'
model = 'openai/gpt-5.4'

[providers.openrouter.headers]
HTTP-Referer = 'https://github.com/philpem/youtube-watchlater-tidy'
X-Title = 'youtube-watchlater-tidy'
```

```bash
export OPENROUTER_API_KEY='...'
```

The preset uses `https://openrouter.ai/api/v1` and `OPENROUTER_API_KEY`.

Check configuration:

```bash
watchlater-llm --config watchlater.toml providers
watchlater-llm --config watchlater.toml probe --provider ollama-local
watchlater-llm --config watchlater.toml prompt
```

See [`docs/llm.md`](docs/llm.md).

## 9. First-pass LLM classification

Inspect a small target first:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --dry-run
```

Then classify:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5
```

The classifier only considers unresolved videos. It can suggest action, topic/type, timeliness, quality/confidence, destination, `needs_description` and `needs_transcript`.

Normal classification uses an exact-run cache. Force another historical run or disable storage:

```bash
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --refresh
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --no-store
```

Inspect runs:

```bash
watchlater-llm --config watchlater.toml results
watchlater-llm --config watchlater.toml results --run-id 12
```

A later human/rule decision takes precedence without rewriting old LLM evidence.

## 10. Description escalation

For a first-pass run that requested descriptions:

```bash
watchlater-metadata enrich --llm-needs-description --run-id 12 --dry-run
watchlater-metadata enrich --llm-needs-description --run-id 12
```

Other selective metadata targets:

```bash
watchlater-metadata enrich --missing-description
watchlater-metadata enrich --selection 12
watchlater-metadata enrich --video-id ID1 --video-id ID2
```

No media is downloaded. Inspect evidence with:

```bash
watchlater-metadata show VIDEO_ID
watchlater-metadata show VIDEO_ID --raw
```

Refine the exact parent run after descriptions exist:

```bash
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local --dry-run

watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local
```

Only the parent run's `needs_description=true` videos that remain unresolved and now have a description are sent. The new child run retains parent-run context and the old run remains unchanged.

Descriptions are capped at 4000 characters by default; use `--max-description-chars` to override. The refinement should request a transcript only when spoken content is still materially necessary.

See [`docs/rich-metadata.md`](docs/rich-metadata.md) and [`docs/llm-classification.md`](docs/llm-classification.md).

## 11. Caption/transcript acquisition

If a stored first-pass or description-refinement run returns `needs_transcript=true`, fetch captions only for that subset:

```bash
watchlater-transcript fetch --llm-needs-transcript --run-id CHILD_RUN_ID --dry-run
watchlater-transcript fetch --llm-needs-transcript --run-id CHILD_RUN_ID
```

Other explicit targets:

```bash
watchlater-transcript fetch --video-id VIDEO_ID
watchlater-transcript fetch --selection 12
watchlater-transcript fetch --all --limit 10
```

Policy:

1. prefer matching normal/manual subtitles;
2. fall back to automatic captions when allowed;
3. otherwise cache `not_found` as evidence only — absence is not a quality judgement.

English is the default language. Ordered alternatives are repeatable:

```bash
watchlater-transcript fetch --video-id VIDEO_ID \
    --language en-GB --language en --language fr
```

Disable automatic-caption fallback with `--no-auto`.

The transcript cache key includes language preferences and automatic-caption policy. Successful and `not_found` lookups are skipped for the same policy unless `--refresh` is used.

The selected caption is fetched from yt-dlp's subtitle metadata, preferring JSON3 and falling back to WebVTT. Video/audio media is never downloaded by this command. Normalized text, segment/timestamp evidence, source/language and the raw caption payload are retained.

Inspect a transcript:

```bash
watchlater-transcript show VIDEO_ID
watchlater-transcript show VIDEO_ID --raw
```

See [`docs/transcripts.md`](docs/transcripts.md).

**Transcript-aware LLM refinement is not implemented yet.** Caption acquisition is the evidence stage for that next step.

## 12. Recommended run

```bash
# Export/import
yt-dlp --cookies-from-browser firefox --flat-playlist --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' > watch-later.json
watchlater import watch-later.json

# Repair/recover metadata gaps
watchlater enrich --missing-creator
watchlater recover --unavailable --limit 20

# Cheap triage
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3
# ... select/action/save rules ...

# Cheap semantic title evidence
watchlater-dearrow enrich --all --remaining

# First LLM pass
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --dry-run
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5

# Suppose the first pass was run 12
watchlater-metadata enrich --llm-needs-description --run-id 12
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local

# Suppose the description child run was 18 and still requests transcripts
watchlater-transcript fetch --llm-needs-transcript --run-id 18

# Inspect stored evidence/results; human decisions remain authoritative
watchlater-llm --config watchlater.toml results --run-id 18
watchlater creators --remaining
```

## 13. Cache/refresh summary

- `watchlater recover` — normal batches skip cached IDs before `--limit`.
- `watchlater-dearrow` — cached found/not-found; TTL via `--max-age` or explicit `--refresh`.
- `watchlater-metadata` — successful yt-dlp observations cached; `--refresh` refetches target.
- `watchlater-transcript` — cache identity includes language/automatic policy; found/not-found cached.
- `watchlater-llm classify` / `refine-description` — exact provider+prompt+evidence runs cached; `--refresh` appends a new run and `--no-store` bypasses persistence.

Use `--dry-run` before large network/provider operations where available.

## 14. Current limitations

- Local `move`, `archive` and `delete` decisions do not yet change YouTube.
- Playlist synchronization/execution is not implemented yet.
- Caption/transcript acquisition is implemented, but transcript-aware LLM refinement is not yet implemented.
- Audio transcription is deliberately not used as an automatic fallback.
- The local HTML human-review report is not implemented yet.

The destructive "clear all Watch Later" browser-console snippet in the main README is separate from this selective workflow. Keep an export/backup first.

## 15. Detailed documentation

- [`docs/README.md`](docs/README.md) — documentation index.
- [`docs/dearrow.md`](docs/dearrow.md) — DeArrow evidence and trust rules.
- [`docs/llm.md`](docs/llm.md) — providers, prompt profiles and OpenAI-compatible transport.
- [`docs/llm-classification.md`](docs/llm-classification.md) — classification/refinement validation, persistence and cache behavior.
- [`docs/rich-metadata.md`](docs/rich-metadata.md) — selective full yt-dlp metadata/description enrichment.
- [`docs/transcripts.md`](docs/transcripts.md) — caption source policy, language selection and transcript cache.

Use `COMMAND --help` as the definitive option reference for the installed version.
