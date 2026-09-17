# youtube-watchlater-tidy user guide

This is the operating guide for the implemented workflow. Detailed references live in
[`docs/`](docs/).

The project is deliberately conservative: imported Watch Later snapshots are evidence,
human/rule decisions outrank LLM suggestions, and YouTube-writing commands require explicit
execution flags. Keep backups of both `watch-later.json` and `watchlater.sqlite3`.

## 1. Workflow at a glance

```text
Watch Later export
      |
      v
immutable SQLite snapshot
      |
      +--> metadata repair / archive recovery
      +--> creator/title/keyword triage + saved rules
      +--> DeArrow alternate titles
      |
      v
LLM first pass over unresolved videos
      |
      +--> descriptions -> description refinement
      +--> captions     -> transcript refinement
      |
      v
self-contained HTML human review
      |
      v
current reviewed/rule decisions
      |
      +--> move -> one or more destination playlists -> API / Playwright execution --+
      |                                                                         |
      +--> delete/archive -------------------------------------------------------+
                                                                                |
                                                                                v
                                                                   Watch Later removal plan
                                                                                |
                                                                                v
                                                             explicit Playwright execution
```

Important invariants:

1. Enrichment never rewrites imported snapshot fields.
2. LLM output is advisory evidence, not a `decision_event`.
3. Playlist and Watch Later execution bind to exact decision-event IDs; changed decisions make old plans stale.
4. A `move` is never removed from Watch Later until **every destination on that exact move decision** is confirmed inserted or live-present.
5. API/browser execution is dry-run by default.

## 2. Install

Core install:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Optional normal-playlist API support:

```bash
pip install -e '.[youtube-api]'
```

Optional browser support for destination playlists and Watch Later:

```bash
pip install -e '.[browser]'
playwright install chromium
```

Important commands:

- `watchlater` — import, reports, selections, rules, metadata repair and archive recovery.
- `watchlater-dearrow` — DeArrow alternate-title enrichment.
- `watchlater-metadata` — selective full yt-dlp metadata/description enrichment.
- `watchlater-transcript` — selective caption acquisition.
- `watchlater-llm` / `watchlater-llm-transcript` — LLM classification/refinement.
- `watchlater-review` — local HTML review and human override import.
- `watchlater-playlist` — destination assignment, inventory, planning and API/Playwright execution.
- `watchlater-remove` — selective Watch Later planning and Playwright execution.

Most commands default to `watchlater.sqlite3`.

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

The importer preserves exact playlist position and hashes the export, so importing the same file again is idempotent. Later exports become separate snapshots.

## 4. Inspect and repair metadata

```bash
watchlater creators --remaining
watchlater videos --unknown-creator --remaining
watchlater videos --unavailable
```

Repair flat-playlist creator gaps:

```bash
watchlater enrich --missing-creator --dry-run
watchlater enrich --missing-creator
```

Recover private/deleted metadata incrementally:

```bash
watchlater recover --unavailable --limit 10
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

Cached archive results are skipped before `--limit`, so repeated runs advance through the uncached remainder. Prefer explicit targets for `--refresh`.

## 5. Cheap/manual triage first

```bash
watchlater creators --remaining
watchlater select creator CHANNEL_ID --remaining
watchlater selection show

watchlater select title --contains 'Super Mario' --remaining
watchlater select title --regex 'conference|keynote' --remaining --max-duration 2h
watchlater keywords --remaining --ngram 2 --min-count 3
```

Record local decisions:

```bash
watchlater selection action move --playlist 'Queue - Electronics'
watchlater selection action archive
watchlater selection action delete --reason 'stale event/news item'
```

Actions:

- `keep` — leave in Watch Later;
- `review` — unresolved;
- `archive` — worthwhile reference, later remove from Watch Later;
- `delete` — discard candidate, later remove from Watch Later;
- `move` — add to destination playlist(s), then remove from Watch Later only after all are confirmed.

### Multiple destination playlists

The ordinary `watchlater selection action move --playlist ...` command remains the simple single-destination path.

When one selected video/cohort should go to several playlists, record one multi-destination move decision explicitly:

```bash
watchlater-playlist assign \
    --selection 12 \
    --playlist 'Queue - Electronics' \
    --playlist 'Reference - Repairs'
```

`--playlist` may be repeated. Duplicate names are collapsed case-insensitively while preserving the first spelling/order. The first destination is retained in the legacy `decision_events.destination_playlist` field for compatibility; the complete ordered set is stored separately and used by planning/execution.

Changing the decision later supersedes the entire destination set.

Reusable rules remain single-destination unless explicitly expanded later:

```bash
watchlater selection save-rule 'electronics creator' move \
    --playlist 'Queue - Electronics' --priority 20
watchlater rules apply --dry-run
watchlater rules apply
```

Rules only act on unresolved videos and never overwrite existing human decisions.

## 6. DeArrow and LLM classification

```bash
watchlater-dearrow enrich --all --remaining
```

Configure providers using `examples/watchlater.example.toml` and an interest brief. The OpenAI-compatible transport supports Ollama, Unsloth models served through vLLM/Ollama/llama-server, OpenRouter, and compatible remote endpoints.

```bash
watchlater-llm --config watchlater.toml providers
watchlater-llm --config watchlater.toml probe --provider ollama-local

watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --dry-run
watchlater-llm --config watchlater.toml classify \
    --provider ollama-local --limit 20 --batch-size 5
```

LLM results are append-only advisory evidence and are cached by provider/prompt/evidence.

## 7. Description and transcript escalation

For a run that requested descriptions:

```bash
watchlater-metadata enrich --llm-needs-description --run-id 12
watchlater-llm --config watchlater.toml refine-description \
    --run-id 12 --provider ollama-local
```

For a run that requested transcripts:

```bash
watchlater-transcript fetch --llm-needs-transcript --run-id 18
watchlater-llm-transcript --config watchlater.toml \
    --run-id 18 --provider ollama-local
```

Existing manual captions are preferred; automatic captions are fallback. Audio is not transcribed by default.

## 8. Human review

```bash
watchlater-review build review.html
```

Open the self-contained HTML locally, export explicit overrides, validate, then import:

```bash
watchlater-review import watchlater-review-1.json --dry-run
watchlater-review import watchlater-review-1.json
```

Imported overrides become append-only human decision events.

## 9. Plan and execute destination playlist moves

Refresh/import normal-playlist inventory first:

```bash
watchlater-playlist inventory refresh
```

Create a plan from all destinations attached to each current `move` decision:

```bash
watchlater-playlist plan --backend api --output playlist-plan.json
# or
watchlater-playlist plan --backend browser --output playlist-plan.json
watchlater-playlist show
```

For a multi-destination decision, the plan contains one independently checkpointed item per `(video, destination)` pair. Quota estimates count every missing API insertion plus any required playlist creation.

Inspect without writes:

```bash
watchlater-playlist execute --run-id PLAN_ID
```

### API backend

```bash
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5
```

### Playwright backend

Use the shared dedicated automation profile:

```bash
watchlater-playlist browser-login
```

Then apply cautiously:

```bash
watchlater-playlist execute --run-id BROWSER_PLAN_ID \
    --apply --max-writes 3
```

Both executors check live membership before insertion, checkpoint every destination separately, refuse stale decision-event IDs, and resume without repeating confirmed work. A secondary destination is valid because authorization is checked against the complete destination set on the exact current decision.

See [`docs/playlist-sync.md`](docs/playlist-sync.md) and [`docs/playlist-browser.md`](docs/playlist-browser.md).

## 10. Build a Watch Later removal plan

```bash
watchlater-remove plan --output removal-plan.json
watchlater-remove show
```

`delete` and `archive` decisions are directly eligible. A single-destination `move` requires that destination to be confirmed. A multi-destination `move` is eligible only when **every destination** on the same exact decision event has a successful checkpoint:

- `inserted`; or
- `already_present` after a live executor re-check (`attempted_at` non-null).

Planner-time inventory-only membership is never enough. If even one destination remains unconfirmed, the source video stays in Watch Later and the removal plan reports the missing destination(s).

Old removal plans become stale when the current decision event changes.

## 11. Set up the shared Playwright profile

Destination-playlist and Watch Later browser executors intentionally share one dedicated automation profile:

```bash
watchlater-remove login
# or
watchlater-playlist browser-login
```

Default profile:

```text
.watchlater-playwright-profile/
```

It is ignored by Git. Sign in manually, then return to the terminal and press Enter to close it.

## 12. Dry-run selective Watch Later execution

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID
```

Without `--apply`, there is no browser creation and no checkpoint mutation.

## 13. Apply selective Watch Later removal

Real removal requires both explicit flags:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove --max-deletes 3
```

The executor refuses stale plans, rechecks authorization immediately before browser work, finds rows by exact video ID, verifies identity before the destructive click, confirms disappearance, and checkpoints `removed`, `already_absent`, `not_found`, or `failed`.

Useful conservative controls:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove \
    --max-deletes 5 --interval 3 --retries 2 --backoff 3
```

For localized YouTube UIs, specify the actual destructive text:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove \
    --remove-label 'Remove from Watch later'
```

Headed mode is the default. See [`docs/watchlater-removal.md`](docs/watchlater-removal.md).

## 14. Recommended end-to-end sequence

```bash
# export/import
yt-dlp --cookies-from-browser firefox --flat-playlist --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' > watch-later.json
watchlater import watch-later.json

# cheap triage / repair
watchlater enrich --missing-creator
watchlater recover --unavailable --limit 20
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3

# semantic pass as needed
watchlater-dearrow enrich --all --remaining
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20

# review
watchlater-review build review.html
watchlater-review import watchlater-review-1.json --dry-run
watchlater-review import watchlater-review-1.json

# optional explicit multi-destination assignment for an already selected cohort
watchlater-playlist assign --selection 12 \
    --playlist 'Queue - Electronics' \
    --playlist 'Reference - Repairs'

# execute all reviewed/rule-approved move destinations first
watchlater-playlist inventory refresh
watchlater-playlist plan --backend api
watchlater-playlist execute --run-id PLAN_ID
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5

# only fully confirmed moves become source-removal candidates
watchlater-remove plan
watchlater-remove execute --run-id REMOVAL_PLAN_ID
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove --max-deletes 3
```

## 15. Current limitations

- Saved rules and LLM destination proposals currently express one destination; multiple destinations are an explicit human assignment through `watchlater-playlist assign`.
- YouTube UI selectors/text can change without notice; browser execution deliberately fails rather than guessing when identity/menu checks do not match.
- Browser playlist creation relies on YouTube's normal private default for private playlists; non-private creation is attempted only when a recognizable privacy control is present.
- Transcript escalation uses existing captions only; audio transcription is not performed by default.

## 16. Detailed documentation

- [`docs/dearrow.md`](docs/dearrow.md) — alternate-title trust/privacy.
- [`docs/llm.md`](docs/llm.md) — LLM provider configuration.
- [`docs/llm-classification.md`](docs/llm-classification.md) — evidence/validation/history.
- [`docs/rich-metadata.md`](docs/rich-metadata.md) — selective descriptions/metadata.
- [`docs/transcripts.md`](docs/transcripts.md) — caption acquisition/refinement.
- [`docs/review.md`](docs/review.md) — static HTML human review.
- [`docs/playlist-sync.md`](docs/playlist-sync.md) — destination assignment/planning/execution.
- [`docs/playlist-browser.md`](docs/playlist-browser.md) — destination Playwright execution.
- [`docs/watchlater-removal.md`](docs/watchlater-removal.md) — selective source removal.

Use `COMMAND --help` as the definitive option reference for the installed version.
