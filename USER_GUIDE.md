# youtube-watchlater-tidy user guide

This is the operating guide for the **implemented** `youtube-watchlater-tidy` workflow. Detailed reference notes live in [`docs/`](docs/).

The tool is deliberately conservative: the current normal workflow analyses and plans changes locally. It does **not** yet remove videos from Watch Later or execute playlist moves on YouTube. Local `delete`, `archive` and `move` actions are plans for a later execution stage.

## 1. Mental model and safety

The workflow is a progressive funnel:

```text
Watch Later export
      |
      v
immutable SQLite snapshot
      |
      +--> repair missing live metadata
      +--> recover private/deleted metadata
      +--> creator / title / keyword cohort triage
      +--> reusable rules
      +--> DeArrow alternate titles
      |
      v
LLM first pass over unresolved videos
      |
      +--> confident suggestion -------------------------------+
      |                                                        |
      +--> needs_description                                   |
                |                                              |
                v                                              |
        selective yt-dlp metadata                              |
                |                                              |
                v                                              |
        description-aware LLM child run                        |
                |                                              |
                +--> needs_transcript                           |
                          |                                     |
                          v                                     |
                  selective caption acquisition                |
                          |                                     |
                          v                                     |
                  transcript-aware LLM child run --------------+
                                                               |
                                                               v
                                                   local human review
                                                               |
                                                               v
                                             playlist move planning
                                                               |
                                                               v
                                               later YouTube execution
```

Safety rules:

1. **Imported snapshots are evidence.** Enrichment never rewrites imported title/channel/position data.
2. **Human and saved-rule decisions outrank LLM suggestions.** LLM output is advisory and stored separately from `decision_events`.
3. **Actions are local plans.** `delete`, `archive` and `move` currently do not mutate YouTube.
4. **Network work is selective and cached.** Prefer explicit cohorts and `--dry-run`.
5. **Execution plans bind to exact decision-event IDs.** If a reviewed decision changes later, the old playlist plan becomes stale rather than silently replaying it.
6. **History is retained.** Decisions, metadata observations, archive lookups, transcript observations, LLM runs and playlist plans are append-only where practical.

Keep backups of both the original `watch-later.json` and `watchlater.sqlite3`.

## 2. Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Installed commands:

- `watchlater` — import, reports, selections, rules and archive recovery.
- `watchlater-dearrow` — DeArrow alternate-title enrichment.
- `watchlater-metadata` — selective full yt-dlp metadata/description enrichment.
- `watchlater-transcript` — selective existing-caption acquisition.
- `watchlater-llm` — provider setup, first-pass classification, description refinement and stored LLM runs.
- `watchlater-llm-transcript` — transcript-aware LLM refinement.
- `watchlater-review` — build a local HTML review report and import explicit human overrides.
- `watchlater-playlist` — import normal-playlist inventory and persist dry-run move plans/checkpoints.

Most commands default to `watchlater.sqlite3`. Put `--db PATH` before the subcommand when using another catalogue.

## 3. Export and import Watch Later

Use a logged-in browser profile and a flat yt-dlp export:

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

The exact export order is stored as snapshot position. Do not assume playlist position means oldest/newest unless you deliberately established that ordering in YouTube first.

The importer hashes the source file, so importing the exact same export again is idempotent. Later exports become new snapshots rather than overwriting old evidence.

## 4. Inspect and repair metadata

Start with:

```bash
watchlater creators --remaining
watchlater videos --unknown-creator --remaining
watchlater videos --unavailable
```

`--remaining` means there is no current human/rule decision.

For live videos whose flat export lacks creator metadata:

```bash
watchlater enrich --missing-creator --dry-run
watchlater enrich --missing-creator
```

Target a specific live video with:

```bash
watchlater enrich --video-id VIDEO_ID
watchlater enrich --video-id VIDEO_ID --refresh
```

Full yt-dlp metadata is stored as a separate observation; the imported snapshot row remains unchanged.

## 5. Recover private/deleted videos

Watch Later can retain a video ID after the video becomes private/deleted. Recovery uses FindYouTubeVideo discovery and currently tries metadata in this order:

1. Filmot data already returned by FindYouTubeVideo.
2. PreserveTube metadata when discovery says it has a copy and Filmot yielded nothing.
3. A specific Wayback watch-page capture discovered by FindYouTubeVideo.

Run incrementally:

```bash
watchlater recover --unavailable --limit 10
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

Cached results are skipped **before** `--limit`, so repeating the command advances to the next uncached batch.

Inspect one cached result:

```bash
watchlater recovery VIDEO_ID
watchlater recovery VIDEO_ID --raw
```

Refresh explicit targets rather than relying on broad refreshes:

```bash
watchlater recover --video-id ID1 --video-id ID2 --refresh
watchlater recover --selection 12 --refresh
watchlater recover --unavailable --min-position 4908 --max-position 4920 --refresh
```

## 6. Cheap/manual triage first

Use obvious cohorts before spending LLM calls.

### Creators

```bash
watchlater creators --remaining
watchlater select creator CHANNEL_ID --remaining
watchlater selection show
```

Prefer a stable channel ID over a display name.

### Titles

```bash
watchlater select title --contains 'Super Mario' --remaining
watchlater select title --regex 'conference|keynote' --remaining --max-duration 2h
```

### Keyword/phrase discovery

```bash
watchlater keywords --remaining --ngram 1 --min-count 3
watchlater keywords --remaining --ngram 2 --min-count 3
watchlater keywords --remaining --ngram 3 --min-count 3
```

The default report suppresses common/function-word phrases and series boilerplate while retaining technical terms such as `C++`, `V.34` and `Z80`.

### Record a local action

```bash
watchlater selection action move --playlist 'Queue - Electronics'
watchlater selection action archive
watchlater selection action delete --reason 'stale event/news item'
```

Actions:

- `keep` — leave in Watch Later.
- `review` — do nothing yet.
- `archive` — worthwhile reference, eventually remove from Watch Later.
- `delete` — discard candidate, eventually remove from Watch Later.
- `move` — eventually add to the destination first, then remove from Watch Later.

These commands only change the local catalogue.

Undo while retaining history:

```bash
watchlater selection undo
```

Export a cohort when useful:

```bash
watchlater selection export --format json --output cohort.json
watchlater selection export --format csv --output cohort.csv
```

## 7. Reusable rules

Save a useful cohort selector for future snapshots:

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

## 8. DeArrow alternate titles

Add trusted alternate-title evidence to unresolved videos:

```bash
watchlater-dearrow enrich --all --remaining
```

Other targets:

```bash
watchlater-dearrow enrich --selection 12
watchlater-dearrow enrich --video-id VIDEO_ID
```

Inspect/cache controls:

```bash
watchlater-dearrow show VIDEO_ID
watchlater-dearrow enrich --all --max-age 7d
watchlater-dearrow enrich --video-id VIDEO_ID --refresh
watchlater-dearrow enrich --all --remaining --hash-prefix
```

The imported YouTube title remains separate. Only a trusted first DeArrow submission (`locked` or non-negative votes) becomes the preferred **alternate** title. `original=true` means the original YouTube title remains preferred.

See [`docs/dearrow.md`](docs/dearrow.md).

## 9. Configure an LLM provider

Copy the example files:

```bash
cp examples/watchlater.example.toml watchlater.toml
cp examples/interests.example.md interests.md
```

The interest file is plain prose/Markdown describing what you find useful. The application adds its own fixed task/schema instructions.

### Ollama

```toml
[providers.ollama-local]
preset = 'ollama'
model = 'qwen3:14b'
structured_mode = 'json_object'
```

Default endpoint: `http://127.0.0.1:11434/v1`.

### Unsloth models

Serve an exported Unsloth model through an OpenAI-compatible engine such as vLLM, llama-server or Ollama:

```toml
[providers.unsloth-local]
preset = 'unsloth'
model = 'my-org/my-unsloth-model'
structured_mode = 'json_schema'
```

The convenience preset defaults to `http://127.0.0.1:8000/v1`; override `base_url` if needed.

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

### Generic OpenAI-compatible endpoint

```toml
[providers.remote]
preset = 'generic'
base_url = 'https://llm.example.invalid/v1'
model = 'example-model'
api_key_env = 'WATCHLATER_LLM_API_KEY'
```

Literal API keys/secrets in TOML are rejected.

Verify configuration and the rendered prompt:

```bash
watchlater-llm --config watchlater.toml providers
watchlater-llm --config watchlater.toml probe --provider ollama-local
watchlater-llm --config watchlater.toml prompt
watchlater-llm --config watchlater.toml prompt --hash-only
```

See [`docs/llm.md`](docs/llm.md).

## 10. First-pass LLM classification

Inspect the exact cheap evidence first:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --dry-run
```

Then classify:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5
```

The classifier considers unresolved videos only and returns validated suggestions including action, topic/content type, timeliness, confidence/quality, destination proposal, `needs_description`, and `needs_transcript`.

Normal classification uses an exact-run cache keyed by output-affecting provider settings, prompt hash and the whole ordered evidence target.

```bash
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --refresh
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --no-store
watchlater-llm --config watchlater.toml results --run-id 12
```

A later human/rule decision simply takes precedence over the historical suggestion.

See [`docs/llm-classification.md`](docs/llm-classification.md).

## 11. Description escalation

If parent run 12 contains `needs_description=true`, fetch only those descriptions:

```bash
watchlater-metadata enrich --llm-needs-description --run-id 12 --dry-run
watchlater-metadata enrich --llm-needs-description --run-id 12
```

Then reclassify exactly that parent run:

```bash
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local --dry-run
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local
```

The child input contains the previous evidence/classification plus description and provenance. Descriptions are capped at 4000 characters by default; use `--max-description-chars` to change that.

The child result is another append-only LLM run with `stage=description_refinement` and `parent_run_id` in its context.

## 12. Caption/transcript acquisition

If an LLM run contains `needs_transcript=true`, fetch existing captions only for that subset:

```bash
watchlater-transcript fetch --llm-needs-transcript --run-id 18 --dry-run
watchlater-transcript fetch --llm-needs-transcript --run-id 18
```

The default policy is manual subtitles first, then matching automatic captions as fallback, otherwise cached `not_found`. Missing captions are **not** treated as evidence of poor video quality.

Ordered language preferences can be supplied:

```bash
watchlater-transcript fetch --video-id VIDEO_ID \
    --language en-GB --language en --language fr
```

Disable automatic captions with `--no-auto`.

No video/audio is downloaded. The cache retains normalized transcript text, segments/timestamps, source type, language, format, raw caption payload, fetch time and request-policy hash.

See [`docs/transcripts.md`](docs/transcripts.md).

## 13. Transcript-aware LLM refinement

After captions have been cached for parent run 18:

```bash
watchlater-llm-transcript \
    --config watchlater.toml \
    --run-id 18 \
    --provider ollama-local \
    --dry-run

watchlater-llm-transcript \
    --config watchlater.toml \
    --run-id 18 \
    --provider ollama-local
```

Only parent-run rows that both have `needs_transcript=true` and remain unresolved are eligible. Videos with no successful cached transcript are reported and skipped.

Long transcripts default to a 12,000-character model-side budget. The tool samples the **beginning, middle and end** deterministically rather than taking only the front:

```bash
watchlater-llm-transcript \
    --config watchlater.toml \
    --run-id 18 \
    --max-transcript-chars 24000
```

The original cached transcript remains untouched. Changing the budget changes the evidence hash/cache key. The result is another append-only child run with `stage=transcript_refinement` and `parent_run_id` in its context.

## 14. Human review report

After cheap rules and LLM refinement have done the bulk work, generate a self-contained local report:

```bash
watchlater-review build review.html
```

Open `review.html` in a normal browser. The report shows imported/recovered/DeArrow metadata, channel/duration/views, the **current human/rule decision**, and the **latest stored LLM suggestion** in separate columns.

The page supports search/filtering by current action, LLM action, topic and maximum confidence, plus sorting by position, confidence or views.

For each row, choose an explicit human override (`keep`, `review`, `archive` or `delete`) and optionally add a note. Rows left at `no override` are not exported.

Press **Export explicit overrides**. The page downloads a versioned JSON file containing the snapshot ID and exact video IDs you explicitly reviewed.

Validate it before writing SQLite:

```bash
watchlater-review import watchlater-review-1.json --dry-run
```

Then apply:

```bash
watchlater-review import watchlater-review-1.json
```

Imported overrides become append-only `decision_events` with `source=human-review-report`. The importer rejects foreign/missing video IDs before writing anything and re-importing an identical current review decision is idempotent.

The report does **not** change YouTube. Existing/proposed `move` actions are displayed, but this first review UI only creates `keep`, `review`, `archive` and `delete` overrides.

See [`docs/review.md`](docs/review.md).

## 15. Plan destination-playlist synchronization

Actual playlist writes are not implemented yet, but move execution now has a persistent dry-run/checkpoint layer.

### Import/refresh normal-playlist inventory

Planning refuses to treat an unknown destination as a missing playlist unless an inventory has been imported first.

Start from the example format:

```bash
cp examples/playlist-inventory.example.json playlist-inventory.json
# replace the example IDs/titles/items with a current inventory
watchlater-playlist inventory import playlist-inventory.json
watchlater-playlist inventory show
```

The inventory records normal playlist IDs/titles/privacy and known video membership. Later API/browser inventory refreshers will populate the same cache automatically.

### Build a plan from current `move` decisions

```bash
watchlater-playlist plan --backend api
```

Only **current local `move` decisions** are executable input. LLM move proposals do not enter the plan merely because a model suggested them.

Destination handling is conservative:

- exactly one title match in the inventory → existing playlist ID;
- no title match → `create_planned` (private by default);
- duplicate matching titles → planning stops as ambiguous.

If the inventory already contains the video in the resolved destination, the item becomes `already_present` and requires no insertion.

### Quota estimate

Current defaults use 50 units for each planned `playlists.insert` and `playlistItems.insert`, with a configurable 10,000-unit allowance:

```bash
watchlater-playlist plan \
    --backend api \
    --quota-limit 10000 \
    --playlist-create-cost 50 \
    --playlist-insert-cost 50
```

If the estimate exceeds the configured allowance, the plan is still persisted/printed for inspection but the CLI returns non-zero unless `--allow-over-quota` is explicitly supplied.

A browser-backend plan uses the same operations/checkpoints but reports zero API-write quota:

```bash
watchlater-playlist plan --backend browser
```

### Inspect staleness before future execution

Every planned video stores the exact decision-event ID that authorized its move. If the current decision changes afterward, the old plan item is shown as stale:

```bash
watchlater-playlist show
watchlater-playlist show --run-id 4
```

A future executor must refuse stale items instead of replaying an obsolete move.

You can also write the exact stored plan JSON:

```bash
watchlater-playlist plan --output playlist-plan.json
```

This tranche does **not** create playlists or add videos yet. See [`docs/playlist-sync.md`](docs/playlist-sync.md).

## 16. Recommended end-to-end run

```bash
# export/import
yt-dlp --cookies-from-browser firefox --flat-playlist --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' > watch-later.json
watchlater import watch-later.json

# repair/recover metadata gaps
watchlater enrich --missing-creator
watchlater recover --unavailable --limit 20

# cheap triage
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3
# ... select obvious cohorts, inspect, action them, save useful rules ...

# alternate-title evidence
watchlater-dearrow enrich --all --remaining

# first-pass LLM
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5

# suppose the first-pass run is 12
watchlater-metadata enrich --llm-needs-description --run-id 12
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local

# suppose the description child run is 18
watchlater-transcript fetch --llm-needs-transcript --run-id 18
watchlater-llm-transcript --config watchlater.toml \
    --run-id 18 --provider ollama-local

# review the catalogue locally and import explicit overrides
watchlater-review build review.html
# ... review in browser and export JSON ...
watchlater-review import watchlater-review-1.json --dry-run
watchlater-review import watchlater-review-1.json

# after move destinations have been reviewed and a current normal-playlist
# inventory has been prepared, build an execution-neutral plan
watchlater-playlist inventory import playlist-inventory.json
watchlater-playlist plan --backend api --output playlist-plan.json
watchlater-playlist show

# inspect remaining work
watchlater creators --remaining
```

## 17. Cache/refresh and planning cheat sheet

`--refresh` means “perform a new network/provider operation despite a cache”, after selecting the target.

- `watchlater recover` — cache keyed by archive lookup/video.
- `watchlater-dearrow` — cache found/not-found; `--max-age` supplies TTL behavior.
- `watchlater-metadata` — cache successful yt-dlp metadata observations.
- `watchlater-transcript` — cache keyed by video plus language/automatic-caption policy.
- LLM first/refinement stages — exact provider + prompt + evidence cache; `--refresh` stores another historical run; `--no-store` bypasses persistence.
- `watchlater-review import` — identical current `human-review-report` action/reason is treated as unchanged rather than appended again.
- `watchlater-playlist plan` — creates a new persistent plan from the current decisions and current imported playlist inventory; `show` re-checks those authorizing decision-event IDs for staleness.

When available, use `--dry-run` before a large network/provider/import operation.

## 18. Current limitations

- **Selective YouTube execution is not yet implemented.** Local `move`, `archive` and `delete` decisions do not change YouTube.
- **Playlist synchronization planning/checkpointing is implemented, but the API/OAuth and browser executors are not yet implemented.** An inventory and plan do not prove that an external YouTube write occurred.
- Transcript escalation uses existing captions only. It deliberately does **not** download/transcribe audio by default.
- The HTML report is deliberately a local static file; it exports JSON for explicit CLI import rather than running a privileged local web service.

The destructive “clear all Watch Later” browser-console snippet in the main README is separate from this selective workflow and should only be used after keeping an export/backup.

## 19. Detailed documentation

- [`docs/dearrow.md`](docs/dearrow.md) — DeArrow API, trust rules, privacy and attribution.
- [`docs/llm.md`](docs/llm.md) — provider configuration, interest profiles and OpenAI-compatible transport.
- [`docs/llm-classification.md`](docs/llm-classification.md) — evidence, validation, append-only LLM history and exact-run caching.
- [`docs/rich-metadata.md`](docs/rich-metadata.md) — selective yt-dlp metadata/description enrichment.
- [`docs/transcripts.md`](docs/transcripts.md) — caption acquisition and transcript-aware refinement.
- [`docs/review.md`](docs/review.md) — self-contained HTML review and explicit human override import.
- [`docs/playlist-sync.md`](docs/playlist-sync.md) — playlist inventory, dry-run move planning, quota estimates and checkpoint state.

Use `COMMAND --help` as the definitive option reference for the installed version.
