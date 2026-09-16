# youtube-watchlater-tidy user guide

This is the operating guide for the **implemented** workflow. Detailed reference notes live in [`docs/`](docs/).

The project is deliberately conservative: imported Watch Later snapshots are evidence; human/rule decisions outrank LLM suggestions; and YouTube-writing commands require an explicit execution step. Keep backups of both `watch-later.json` and `watchlater.sqlite3`.

## 1. Workflow at a glance

```text
Watch Later export
      |
      v
immutable SQLite snapshot
      |
      +--> repair missing live metadata
      +--> recover private/deleted metadata
      +--> creator/title/keyword cohort triage
      +--> reusable rules
      +--> DeArrow alternate titles
      |
      v
LLM first pass over unresolved videos
      |
      +--> needs_description -> selective metadata -> description refinement
      |
      +--> needs_transcript  -> selective captions -> transcript refinement
      |
      v
self-contained HTML human review
      |
      v
current reviewed/rule decisions
      |
      +--> move -> destination playlist plan -> optional YouTube Data API execution
      |
      +--> delete/archive -> Watch Later removal is still a separate future executor
```

Important invariants:

1. Enrichment never rewrites imported snapshot fields.
2. LLM suggestions are advisory evidence, never automatic `decision_events`.
3. Playlist execution only consumes **current local `move` decisions**, not raw LLM proposals.
4. Playlist plans bind each item to the exact authorizing decision-event ID; changed decisions make an old plan stale.
5. API execution is dry-run by default and performs writes only with `--apply`.
6. Watch Later removal is not performed by playlist synchronization.

## 2. Install

Core install:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

For authenticated normal-playlist synchronization through Google's YouTube Data API:

```bash
pip install -e '.[youtube-api]'
```

Installed commands include:

- `watchlater` — import, reports, selections, rules, live metadata repair and archive recovery.
- `watchlater-dearrow` — DeArrow alternate-title enrichment.
- `watchlater-metadata` — selective rich yt-dlp metadata/description enrichment.
- `watchlater-transcript` — selective existing-caption acquisition.
- `watchlater-llm` — provider setup, first-pass classification and description refinement.
- `watchlater-llm-transcript` — transcript-aware refinement.
- `watchlater-review` — local HTML review plus explicit human-decision import.
- `watchlater-playlist` — playlist inventory, planning and optional API execution.

Most commands default to `watchlater.sqlite3`. Put `--db PATH` before the subcommand to use another catalogue.

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

Change `firefox` if needed. The importer hashes the source file, so re-importing the exact same export is idempotent. Later exports become separate snapshots.

Playlist position is preserved exactly, but should not be interpreted as age unless you deliberately established that ordering before export.

## 4. Inspect and repair metadata

```bash
watchlater creators --remaining
watchlater videos --unknown-creator --remaining
watchlater videos --unavailable
```

Repair otherwise-live videos whose flat export lacks creator metadata:

```bash
watchlater enrich --missing-creator --dry-run
watchlater enrich --missing-creator
```

Or target one ID:

```bash
watchlater enrich --video-id VIDEO_ID
watchlater enrich --video-id VIDEO_ID --refresh
```

The imported snapshot is unchanged; yt-dlp data is stored as a separate observation.

## 5. Recover private/deleted videos

Run archive recovery incrementally:

```bash
watchlater recover --unavailable --limit 10
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

Normal runs skip cached found/not-found results before applying `--limit`, so repeating the command advances through the uncached remainder.

Inspect one cached lookup:

```bash
watchlater recovery VIDEO_ID
watchlater recovery VIDEO_ID --raw
```

For refreshes, prefer an explicit target:

```bash
watchlater recover --video-id ID1 --video-id ID2 --refresh
watchlater recover --selection 12 --refresh
watchlater recover --unavailable --min-position 4908 --max-position 4920 --refresh
```

Recovery currently uses FindYouTubeVideo discovery, Filmot metadata, PreserveTube fallback and specific Wayback watch-page captures. Recovered values remain separate from the unavailable source row.

## 6. Cheap/manual triage before LLMs

Creators:

```bash
watchlater creators --remaining
watchlater select creator CHANNEL_ID --remaining
watchlater selection show
```

Titles:

```bash
watchlater select title --contains 'Super Mario' --remaining
watchlater select title --regex 'conference|keynote' --remaining --max-duration 2h
```

Keyword/phrase discovery:

```bash
watchlater keywords --remaining --ngram 1 --min-count 3
watchlater keywords --remaining --ngram 2 --min-count 3
watchlater keywords --remaining --ngram 3 --min-count 3
```

Record local decisions:

```bash
watchlater selection action move --playlist 'Queue - Electronics'
watchlater selection action archive
watchlater selection action delete --reason 'stale event/news item'
```

Actions mean:

- `keep` — leave in Watch Later.
- `review` — do nothing yet.
- `archive` — worthwhile reference; eventual Watch Later removal is a separate executor.
- `delete` — discard candidate; eventual Watch Later removal is a separate executor.
- `move` — destination playlist assignment; normal-playlist synchronization can execute the add, but Watch Later removal remains separate.

Undo while preserving history:

```bash
watchlater selection undo
```

Export a cohort when useful:

```bash
watchlater selection export --format json --output cohort.json
watchlater selection export --format csv --output cohort.csv
```

## 7. Reusable rules

```bash
watchlater select creator CHANNEL_ID --remaining
watchlater selection save-rule \
    'electronics creator' \
    move \
    --playlist 'Queue - Electronics' \
    --priority 20

watchlater rules list
watchlater rules apply --dry-run
watchlater rules apply
```

Rules only act on unresolved videos. Existing human decisions are never overwritten. Lower numeric priority runs first, then rule ID.

## 8. DeArrow alternate titles

```bash
watchlater-dearrow enrich --all --remaining
watchlater-dearrow show VIDEO_ID
```

Useful cache/privacy controls:

```bash
watchlater-dearrow enrich --all --max-age 7d
watchlater-dearrow enrich --video-id VIDEO_ID --refresh
watchlater-dearrow enrich --all --remaining --hash-prefix
```

The imported YouTube title remains separate. Only a trusted first DeArrow submission (`locked` or non-negative votes) becomes the preferred alternate title; `original=true` keeps the YouTube title preferred.

See [`docs/dearrow.md`](docs/dearrow.md).

## 9. Configure LLM providers

Copy the examples:

```bash
cp examples/watchlater.example.toml watchlater.toml
cp examples/interests.example.md interests.md
```

The same OpenAI-compatible transport supports Ollama, Unsloth models served through vLLM/llama-server/Ollama, OpenRouter, and arbitrary compatible endpoints.

Example Ollama profile:

```toml
[providers.ollama-local]
preset = 'ollama'
model = 'qwen3:14b'
structured_mode = 'json_object'
```

Example Unsloth/vLLM profile:

```toml
[providers.unsloth-local]
preset = 'unsloth'
model = 'my-org/my-unsloth-model'
structured_mode = 'json_schema'
```

Example OpenRouter profile:

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

Verify the provider and effective prompt:

```bash
watchlater-llm --config watchlater.toml providers
watchlater-llm --config watchlater.toml probe --provider ollama-local
watchlater-llm --config watchlater.toml prompt
watchlater-llm --config watchlater.toml prompt --hash-only
```

Literal API keys in TOML are rejected. See [`docs/llm.md`](docs/llm.md).

## 10. First-pass LLM classification

Inspect exact evidence first:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --dry-run
```

Then classify:

```bash
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5
```

Validated LLM results are append-only advisory evidence. Exact provider+prompt+ordered-evidence runs are cached.

```bash
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --refresh
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20 --no-store
watchlater-llm --config watchlater.toml results --run-id 12
```

## 11. Description refinement

For parent run 12:

```bash
watchlater-metadata enrich --llm-needs-description --run-id 12 --dry-run
watchlater-metadata enrich --llm-needs-description --run-id 12

watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local --dry-run
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local
```

Only unresolved `needs_description=true` rows with an available description are refined. The child LLM run records its parent and evidence provenance.

## 12. Caption acquisition and transcript refinement

Fetch captions only for a parent run's `needs_transcript=true` subset:

```bash
watchlater-transcript fetch --llm-needs-transcript --run-id 18 --dry-run
watchlater-transcript fetch --llm-needs-transcript --run-id 18
```

Manual captions are preferred; automatic captions are fallback unless `--no-auto` is used. Missing captions are not a negative quality signal.

Then refine:

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

Long transcripts use deterministic beginning/middle/end sampling within a configurable character budget. See [`docs/transcripts.md`](docs/transcripts.md).

## 13. Human review

Build a self-contained local report:

```bash
watchlater-review build review.html
```

The report displays current human/rule decisions and latest LLM suggestions separately. Choose explicit `keep`, `review`, `archive` or `delete` overrides and export the JSON file.

Validate before import:

```bash
watchlater-review import watchlater-review-1.json --dry-run
```

Then apply:

```bash
watchlater-review import watchlater-review-1.json
```

Imported overrides become append-only `decision_events` with `source=human-review-report`. Re-importing an identical current review action/reason is idempotent. See [`docs/review.md`](docs/review.md).

## 14. Plan normal-playlist synchronization

Playlist synchronization consumes only current local `move` decisions.

### Offline/manual inventory

```bash
watchlater-playlist inventory import playlist-inventory.json
watchlater-playlist inventory show
```

### Authenticated API inventory

Install the optional extra first:

```bash
pip install -e '.[youtube-api]'
```

Create a Google OAuth **Desktop app** client in a Cloud project with the YouTube Data API enabled and place the downloaded secret at `client_secret.json`, or provide another path.

Refresh live owned playlists/membership:

```bash
watchlater-playlist inventory refresh
```

Defaults:

```text
client secrets: client_secret.json
OAuth token:    .youtube-watchlater-token.json
```

Both default credential/token names are ignored by Git. Override them with `--client-secrets` and `--token`.

### Create a persistent move plan

```bash
watchlater-playlist plan --backend api --output playlist-plan.json
watchlater-playlist show
```

Planning resolves known destination titles to stable playlist IDs, marks missing destinations `create_planned`, records known membership as `already_present`, estimates API quota, and stores the exact decision-event ID authorizing every item.

The default planner assumptions are 50 units per playlist creation/insertion and a 10,000-unit allowance; override them explicitly if your project/account policy differs.

See [`docs/playlist-sync.md`](docs/playlist-sync.md).

## 15. Execute a normal-playlist API plan

Execution is **dry-run by default**:

```bash
watchlater-playlist execute --run-id 4
```

No OAuth client or external write is created in this mode.

After reviewing the plan, apply it explicitly:

```bash
watchlater-playlist execute --run-id 4 --apply
```

A real API run:

- refuses stale plan items before writes;
- refuses an over-quota plan unless `--allow-over-quota` is explicitly repeated;
- re-lists live owned playlists;
- uses stable IDs for destinations known at planning time;
- resolves `create_planned` names against the live account before creating anything;
- refuses ambiguous titles;
- refuses to silently recreate a previously known playlist ID that disappeared;
- re-checks the authorizing decision immediately before each membership/write operation;
- checks live membership immediately before each insertion;
- treats a live duplicate as `already_present` success;
- checkpoints playlist creation, insertion and failures in SQLite;
- updates the local inventory after confirmed writes.

Limit actual writes for a cautious rollout:

```bash
watchlater-playlist execute --run-id 4 --apply --max-writes 5
```

`--max-writes` counts playlist creations + item insertions. Hitting the cap leaves a partial run; rerun the same command later to resume from checkpoints.

Use alternate OAuth files if desired:

```bash
watchlater-playlist execute --run-id 4 --apply \
    --client-secrets ~/private/youtube-client.json \
    --token ~/private/youtube-token.json
```

A confirmed playlist insertion does **not** remove the video from Watch Later. That remains issue #7 and must only happen after destination success for a `move` decision.

## 16. Recommended end-to-end sequence

```bash
# Export/import
yt-dlp --cookies-from-browser firefox --flat-playlist --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' > watch-later.json
watchlater import watch-later.json

# Repair/recover obvious metadata gaps
watchlater enrich --missing-creator
watchlater recover --unavailable --limit 20

# Cheap triage
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3
# ... select/action obvious cohorts; save useful rules ...

# Semantic evidence/classification
watchlater-dearrow enrich --all --remaining
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5
# ... selectively fetch descriptions/captions and refine only when requested ...

# Human review
watchlater-review build review.html
# ... export explicit overrides from the browser ...
watchlater-review import watchlater-review-1.json --dry-run
watchlater-review import watchlater-review-1.json

# Refresh normal playlists and plan reviewed/rule-approved moves
watchlater-playlist inventory refresh
watchlater-playlist plan --backend api
watchlater-playlist execute --run-id PLAN_ID

# Only after reviewing the dry-run:
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5
```

## 17. Current limitations

- Watch Later removal is **not yet implemented** in the selective workflow. `delete`/`archive` are still local decisions, and successful `move` insertion does not remove the source Watch Later item.
- Browser-backed bulk destination-playlist execution is not yet implemented; current real playlist writes use the official API backend.
- One current `move` decision targets one destination playlist; explicit multi-destination execution is not yet modelled.
- Transcript escalation deliberately uses existing captions only; audio download/transcription is not performed by default.

## 18. Detailed documentation

- [`docs/dearrow.md`](docs/dearrow.md) — alternate-title trust/privacy.
- [`docs/llm.md`](docs/llm.md) — LLM provider configuration.
- [`docs/llm-classification.md`](docs/llm-classification.md) — evidence, validation and LLM history.
- [`docs/rich-metadata.md`](docs/rich-metadata.md) — selective descriptions/metadata.
- [`docs/transcripts.md`](docs/transcripts.md) — caption acquisition/refinement.
- [`docs/review.md`](docs/review.md) — static HTML human review.
- [`docs/playlist-sync.md`](docs/playlist-sync.md) — destination inventory, planning and API execution.

Use `COMMAND --help` as the definitive option reference for the installed version.
