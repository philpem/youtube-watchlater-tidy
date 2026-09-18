# Destination playlist synchronization

Playlist synchronization deliberately separates **classification**, **planning**, and **execution**. A stored LLM suggestion is never executable merely because a model proposed `move`; only a current local `move` decision is allowed into a plan.

The implementation supports:

1. destination-playlist inventory from versioned JSON or the authenticated YouTube Data API;
2. single- or multi-destination move decisions;
3. persistent/idempotent planning;
4. quota and stale-decision checks;
5. authenticated YouTube Data API execution;
6. authenticated Playwright execution for quota-heavy browser moves;
7. resumable per-destination/per-video checkpoints;
8. safe hand-off to Watch Later removal only after every requested destination succeeds.

A successful destination insertion never removes the source item by itself.

## Inventory

Refresh owned playlists and known membership through the API:

```bash
pip install -e '.[youtube-api]'
watchlater-playlist inventory refresh
watchlater-playlist inventory show
```

Or import the versioned JSON inventory manually:

```bash
watchlater-playlist inventory import playlist-inventory.json
```

The format is `youtube-watchlater-tidy-playlist-inventory-v1`; see [`examples/playlist-inventory.example.json`](https://github.com/philpem/youtube-watchlater-tidy/blob/master/examples/playlist-inventory.example.json).

Duplicate playlist IDs and duplicate video IDs inside one playlist are rejected. Duplicate titles are retained because YouTube permits them; planning refuses an ambiguous title rather than guessing.

## Record move destinations

The ordinary single-destination workflow remains:

```bash
watchlater selection action move --playlist 'Queue - Electronics'
```

For an explicit one-video/cohort-to-many-playlists decision, use the current selection with repeated `--playlist` options:

```bash
watchlater-playlist assign \
    --selection 12 \
    --playlist 'Queue - Electronics' \
    --playlist 'Reference - Repairs'
```

This records one current `move` decision per selected video with an ordered destination set. Duplicate destination names are collapsed case-insensitively.

For backward compatibility, the first destination is also stored in `decision_events.destination_playlist`. The complete set is stored in `decision_event_destinations` and is authoritative for playlist planning/execution.

Existing databases and older single-destination decisions are backfilled automatically when the multi-destination support is first used.

Changing/superseding the move decision replaces the destination set as a whole.

## Build a move plan

Only current `move` decisions are included. LLM proposals remain advisory until a human/rule workflow creates a current decision.

```bash
watchlater-playlist plan --backend api
# or
watchlater-playlist plan --backend browser
watchlater-playlist show
```

For every destination:

- exactly one inventory title match -> stable existing playlist ID;
- no match -> `create_planned`;
- more than one title match -> planning fails as ambiguous.

For a multi-destination decision, planning expands the decision into one independently checkpointed item for every `(video_id, destination_playlist)` pair. All items retain the same authorizing `decision_events.id`.

New destinations default to private:

```bash
watchlater-playlist plan --backend api --new-playlist-privacy unlisted
```

If the current decision event changes, every item derived from the old event becomes stale.

## API quota planning

API plans default to 50 units per playlist creation and 50 units per playlist-item insertion against a 10,000-unit allowance. These are planner inputs, not schema invariants:

```bash
watchlater-playlist plan \
    --backend api \
    --quota-limit 10000 \
    --playlist-create-cost 50 \
    --playlist-insert-cost 50
```

The estimate counts every missing destination membership independently. Browser plans have zero API quota estimate.

## Dry-run execution

Both backends are dry-run by default:

```bash
watchlater-playlist execute --run-id PLAN_ID
```

No OAuth client or Playwright browser is created unless `--apply` is present.

## API execution

```bash
pip install -e '.[youtube-api]'
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5
```

The API executor verifies live playlists, resolves/creates missing destinations, rechecks the exact current move decision and destination authorization immediately before each membership/write step, checks live membership, avoids duplicate insertion, checkpoints success/failure, and updates the local inventory.

A secondary destination on a multi-destination decision is valid because authorization checks the complete destination set attached to the same exact decision-event ID.

## Playwright execution

For new sign-ins, launch and authenticate Chrome/Chromium yourself, then attach Playwright over CDP rather than logging in inside a Playwright-launched browser:

```bash
pip install -e '.[browser]'

google-chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.local/share/watchlater-chrome"

# after signing in manually in that browser:
watchlater-playlist execute --run-id BROWSER_PLAN_ID \
    --apply --max-writes 3 \
    --cdp-endpoint http://127.0.0.1:9222
```

The Playwright backend uses the same checkpoint model. It discovers exact playlist IDs, verifies exact `v=VIDEO_ID` membership, creates/re-resolves missing destinations, confirms membership after Save actions, and fails rather than guessing on ambiguous identity. The older Playwright-launched persistent-profile mode remains available for already-authenticated profiles when `--cdp-endpoint` is omitted.

See [`playlist-browser.md`](playlist-browser.md) for browser-specific controls, authentication setup and identity checks.

## Write caps, resume and idempotence

`--max-writes` counts actual playlist creations plus video insertions. Read-only live checks do not count. Hitting the cap leaves the run `partial`; repeat the same command later to resume.

Successful `(video,destination)` insertions are not repeated. Failed items remain retriable. Planner-time `already_present` is rechecked live before becoming a completed checkpoint.

## Watch Later coordination

After destination synchronization, build a separate removal plan:

```bash
watchlater-remove plan
watchlater-remove show
```

For `delete` and `archive`, no destination gate applies. For `move`:

- a single-destination decision must have that destination confirmed;
- a multi-destination decision must have **every destination** confirmed for the same exact decision-event ID.

Accepted confirmations are `inserted`, or `already_present` after a live executor check (`attempted_at` non-null). Inventory-only membership does not authorize removal.

If any destination is missing, the removal planner blocks that video and reports the missing destination names. Source removal remains an explicit separate operation.

## Current scope

Both execution backends, Watch Later coordination, and explicit one-video-to-many-destination assignment are implemented. Saved rules and LLM destination proposals remain single-destination; multi-destination assignment is currently an explicit human operation through `watchlater-playlist assign`.

Current quota references:

- <https://developers.google.com/youtube/v3/docs/playlists/insert>
- <https://developers.google.com/youtube/v3/docs/playlistItems/insert>
- <https://developers.google.com/youtube/v3/determine_quota_cost>
