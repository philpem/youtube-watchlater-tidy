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
      +--> move ----------> destination playlist plan/API execution --+
      |                                                              |
      +--> delete/archive --------------------------------------------+
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
3. Playlist and Watch Later execution bind to exact decision-event IDs; changed decisions
   make old plans stale.
4. A `move` is never removed from Watch Later until the same move decision has a confirmed
   destination insertion/live membership checkpoint.
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

Optional Watch Later browser support:

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
- `watchlater-playlist` — normal-playlist inventory, planning and API execution.
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

The importer preserves exact playlist position and hashes the export, so importing the same
file again is idempotent. Later exports become separate snapshots.

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

Cached archive results are skipped before `--limit`, so repeated runs advance through the
uncached remainder. Prefer explicit targets for `--refresh`.

## 5. Cheap/manual triage first

Creators:

```bash
watchlater creators --remaining
watchlater select creator CHANNEL_ID --remaining
watchlater selection show
```

Titles and phrases:

```bash
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
- `move` — add to a destination first, then remove from Watch Later.

Reusable rules:

```bash
watchlater selection save-rule 'electronics creator' move \
    --playlist 'Queue - Electronics' --priority 20
watchlater rules apply --dry-run
watchlater rules apply
```

Rules only act on unresolved videos and never overwrite existing human decisions.

## 6. DeArrow and LLM classification

DeArrow:

```bash
watchlater-dearrow enrich --all --remaining
```

Configure providers using `examples/watchlater.example.toml` and an interest brief. The same
OpenAI-compatible transport supports Ollama, Unsloth models served through vLLM/Ollama/
llama-server, OpenRouter, and compatible remote endpoints.

Verify a provider:

```bash
watchlater-llm --config watchlater.toml providers
watchlater-llm --config watchlater.toml probe --provider ollama-local
```

First pass:

```bash
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

Existing manual captions are preferred; automatic captions are fallback. Audio is not
transcribed by default.

## 8. Human review

```bash
watchlater-review build review.html
```

Open the self-contained HTML file locally, review current decisions and LLM suggestions, and
export explicit overrides. Validate before import:

```bash
watchlater-review import watchlater-review-1.json --dry-run
watchlater-review import watchlater-review-1.json
```

Imported overrides become append-only human decision events.

## 9. Plan and execute destination playlist moves

Install the API extra, create a Google OAuth Desktop client, and refresh owned playlists:

```bash
watchlater-playlist inventory refresh
```

Create a plan from current `move` decisions:

```bash
watchlater-playlist plan --backend api --output playlist-plan.json
watchlater-playlist show
```

Inspect execution without any writes:

```bash
watchlater-playlist execute --run-id PLAN_ID
```

Apply cautiously:

```bash
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5
```

The executor checks live membership before insertion, checkpoints every result, refuses stale
plans, and resumes without repeating confirmed work. See [`docs/playlist-sync.md`](docs/playlist-sync.md).

## 10. Build a Watch Later removal plan

Removal planning consumes current `delete`/`archive` decisions and eligible `move` decisions:

```bash
watchlater-remove plan --output removal-plan.json
watchlater-remove show
```

A `move` is eligible only when its exact current decision has a confirmed destination
checkpoint (`inserted`, or `already_present` after a live re-check). Inventory-only knowledge
is deliberately insufficient.

Old removal plans are reported stale when the current action/destination/decision event
changes.

## 11. Set up the Playwright Watch Later profile

Use a dedicated automation profile, not your normal browser profile:

```bash
watchlater-remove login
```

The default profile directory is:

```text
.watchlater-playwright-profile/
```

It is ignored by Git. Sign in to YouTube manually in the opened browser, return to the
terminal, then press Enter to close it.

## 12. Dry-run selective Watch Later execution

Inspect the latest or selected persisted plan without opening a browser:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID
```

Without `--apply`, there is no browser creation and no checkpoint mutation.

## 13. Apply selective Watch Later removal

Real removal requires **two explicit flags**:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply \
    --confirm-remove
```

Start with a small cap:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove \
    --max-deletes 3
```

The executor:

- refuses stale plans before launching a destructive run;
- rechecks authorization before each browser attempt;
- finds rows by exact YouTube video ID rather than playlist order;
- verifies the row's exact `v=VIDEO_ID` identity before opening its menu;
- verifies that identity again immediately before clicking `Remove from Watch later`;
- confirms the exact row disappeared after the click;
- distinguishes `removed`, `already_absent`, `not_found`, and `failed`;
- retries retriable failures with configurable exponential backoff;
- checkpoints results and skips terminal successes on resume;
- preserves the imported catalogue/snapshot after successful removal.

Default safety controls include a maximum of 10 removals per invocation, a 2-second interval
between confirmed removals, one retry, and conservative scrolling. Override only when needed:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove \
    --max-deletes 5 --interval 3 --retries 2 --backoff 3
```

For localized YouTube UIs, set the destructive menu text explicitly rather than allowing the
tool to guess:

```bash
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove \
    --remove-label 'Remove from Watch later'
```

Headed mode is the default so destructive behavior is visible. `--headless` is available only
for an already validated setup.

See [`docs/watchlater-removal.md`](docs/watchlater-removal.md).

## 14. Recommended end-to-end sequence

```bash
# export/import
yt-dlp --cookies-from-browser firefox --flat-playlist --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' > watch-later.json
watchlater import watch-later.json

# repair/recover metadata and perform cheap triage
watchlater enrich --missing-creator
watchlater recover --unavailable --limit 20
watchlater creators --remaining
watchlater keywords --remaining --ngram 2 --min-count 3

# semantic classification/refinement as needed
watchlater-dearrow enrich --all --remaining
watchlater-llm --config watchlater.toml classify --provider ollama-local --limit 20

# human review
watchlater-review build review.html
watchlater-review import watchlater-review-1.json --dry-run
watchlater-review import watchlater-review-1.json

# execute reviewed/rule-approved moves first
watchlater-playlist inventory refresh
watchlater-playlist plan --backend api
watchlater-playlist execute --run-id PLAN_ID
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5

# then build source-removal plan; unconfirmed moves are blocked automatically
watchlater-remove plan
watchlater-remove execute --run-id REMOVAL_PLAN_ID

# only after reviewing that dry-run
watchlater-remove execute --run-id REMOVAL_PLAN_ID \
    --apply --confirm-remove --max-deletes 3
```

## 15. Current limitations

- Browser-backed **destination-playlist insertion** is not implemented; current real normal-
  playlist writes use the official YouTube Data API backend.
- One current `move` decision targets one destination playlist; explicit multi-destination
  execution is not yet modelled.
- Watch Later UI selectors/text can change without notice; browser execution deliberately
  fails rather than guessing when identity/menu checks do not match.
- Transcript escalation uses existing captions only; audio transcription is not performed by
  default.

## 16. Detailed documentation

- [`docs/dearrow.md`](docs/dearrow.md) — alternate-title trust/privacy.
- [`docs/llm.md`](docs/llm.md) — LLM provider configuration.
- [`docs/llm-classification.md`](docs/llm-classification.md) — evidence/validation/history.
- [`docs/rich-metadata.md`](docs/rich-metadata.md) — selective descriptions/metadata.
- [`docs/transcripts.md`](docs/transcripts.md) — caption acquisition/refinement.
- [`docs/review.md`](docs/review.md) — static HTML human review.
- [`docs/playlist-sync.md`](docs/playlist-sync.md) — destination planning/API execution.
- [`docs/watchlater-removal.md`](docs/watchlater-removal.md) — selective source removal.

Use `COMMAND --help` as the definitive option reference for the installed version.
