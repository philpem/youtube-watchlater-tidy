# Destination playlist synchronization

Issue #10 separates **classification/planning** from the mechanism that eventually changes YouTube. The current implementation provides the execution-neutral planning and checkpoint foundation; API/OAuth and browser writers are later stages.

No command described here currently changes YouTube.

## Why an inventory is required

A destination title that is absent from local knowledge must not automatically be interpreted as “create a new playlist”. Before planning, import a snapshot of the user's normal YouTube playlists and (where known) their current video membership.

The current versioned inventory format is:

```text
youtube-watchlater-tidy-playlist-inventory-v1
```

An example is provided as [`examples/playlist-inventory.example.json`](../examples/playlist-inventory.example.json).

Import it with:

```bash
watchlater-playlist inventory import playlist-inventory.json
```

Inspect the cached inventory:

```bash
watchlater-playlist inventory show
```

Import replaces the previous inventory atomically. Duplicate playlist IDs and duplicate video IDs inside one playlist are rejected. Duplicate **titles** are retained because YouTube allows them; planning refuses to resolve an ambiguous destination title until the inventory/user disambiguates it.

Later API/browser inventory refreshers can populate the same tables without changing the planner.

## What becomes an executable playlist plan

Only current local catalogue decisions with:

```text
action = move
```

are included. Stored LLM `move` suggestions are **not** executable input merely because the model proposed them. They need to become a current reviewed/rule/human decision first.

Create a plan with:

```bash
watchlater-playlist plan
```

or select the future executor explicitly:

```bash
watchlater-playlist plan --backend api
watchlater-playlist plan --backend browser
```

Planning is a dry-run with respect to YouTube. It persists the exact plan and checkpoints in SQLite so a later executor can resume/audit it.

## Destination resolution

For every distinct destination name in the current `move` decisions:

- exactly one inventory title match -> `existing`, with a stable playlist ID;
- no match -> `create_planned`;
- more than one match -> planning fails as ambiguous.

New destinations default to `private`:

```bash
watchlater-playlist plan --new-playlist-privacy private
```

`unlisted` and `public` can be selected explicitly.

## Existing membership and idempotence

If the inventory says a video is already present in its resolved destination, the item is stored as:

```text
already_present
```

and no insertion is included in the API quota estimate.

Other items begin as `planned`. Future executors update these persistent rows to inserted/failed/skipped rather than deriving state from playlist order.

## Decision-event checkpointing and stale plans

Every planned video stores the exact `decision_events.id` that authorized the move. `watchlater-playlist show` compares that ID and destination with the **current** catalogue decision.

If the user changes a decision after planning, the old item becomes:

```json
"stale": true
```

A future executor must refuse stale items instead of replaying an obsolete plan.

Inspect the latest plan:

```bash
watchlater-playlist show
```

or a particular plan:

```bash
watchlater-playlist show --run-id 4
```

## API quota estimate

As of September 2026, Google's YouTube Data API documentation lists:

- `playlists.insert`: 50 units;
- `playlistItems.insert`: 50 units;
- list calls: normally 1 unit.

The planner defaults to 50 units per planned playlist creation/insertion and a 10,000-unit allowance, but all three values are configurable because API quotas can change:

```bash
watchlater-playlist plan \
    --backend api \
    --quota-limit 10000 \
    --playlist-create-cost 50 \
    --playlist-insert-cost 50
```

For an API plan:

```text
estimated quota = new playlist creations × create cost
                + missing video insertions × insert cost
```

An item already present costs zero insertion units in the plan. A browser-backend plan reports zero **API** quota because it will not use those API writes.

If an API estimate exceeds `--quota-limit`, the plan is still stored/printed for inspection but the CLI exits non-zero unless `--allow-over-quota` is given. This is a planning warning only; there is still no external execution in this tranche.

The current quota references are:

- <https://developers.google.com/youtube/v3/docs/playlists/insert>
- <https://developers.google.com/youtube/v3/docs/playlistItems/insert>
- <https://developers.google.com/youtube/v3/determine_quota_cost>

## Exporting the exact plan

The planner always prints JSON. It can also write the exact stored representation to a file:

```bash
watchlater-playlist plan --output playlist-plan.json
```

The stored plan includes:

- snapshot ID;
- executor backend;
- inventory timestamp;
- destination resolution/create status;
- every exact video ID and authorizing decision-event ID;
- existing playlist/item IDs where known;
- insertion/already-present state;
- quota assumptions and estimate.

## What remains for issue #10

This foundation intentionally performs no OAuth or browser mutation yet. Subsequent tranches add:

1. API/OAuth inventory refresh and executor;
2. idempotent normal-playlist create/insert using these checkpoint tables;
3. browser executor for quota-heavy bulk work;
4. coordination with #7 so Watch Later removal occurs only after a `move` destination is confirmed inserted/already-present.
