# Destination playlist synchronization

Playlist synchronization deliberately separates **classification**, **planning**, and **execution**. A stored LLM suggestion is never executable merely because a model proposed `move`; only a current local `move` decision is allowed into a plan.

The current implementation supports:

1. destination-playlist inventory from versioned JSON or the authenticated YouTube Data API;
2. persistent/idempotent move planning;
3. quota and stale-decision checks;
4. authenticated YouTube Data API execution for normal playlists;
5. authenticated Playwright execution for quota-heavy browser moves;
6. resumable per-playlist/per-video checkpoints;
7. safe hand-off to the separate Watch Later removal executor after destination success.

A successful destination insertion does **not** itself remove the source item from Watch Later. `watchlater-remove plan` accepts a `move` only after the exact move decision has a confirmed destination checkpoint (`inserted`, or `already_present` after a live re-check).

## Inventory

Refresh owned playlists and known membership through the API:

```bash
pip install -e '.[youtube-api]'
watchlater-playlist inventory refresh
watchlater-playlist inventory show
```

The versioned JSON inventory can also be imported manually:

```bash
watchlater-playlist inventory import playlist-inventory.json
```

The format remains `youtube-watchlater-tidy-playlist-inventory-v1`; see [`examples/playlist-inventory.example.json`](../examples/playlist-inventory.example.json).

Duplicate playlist IDs and duplicate video IDs inside one playlist are rejected. Duplicate **titles** are retained because YouTube permits them; planning refuses an ambiguous title rather than guessing.

## Build a move plan

Only current catalogue decisions where `action = move` are included. LLM proposals remain advisory until a human/rule workflow turns them into a current decision.

Create either backend from the same local intent:

```bash
watchlater-playlist plan --backend api
watchlater-playlist plan --backend browser
watchlater-playlist show
```

For every destination:

- exactly one inventory title match -> stable existing playlist ID;
- no match -> `create_planned`;
- more than one title match -> planning fails as ambiguous.

New destinations default to private:

```bash
watchlater-playlist plan --backend api --new-playlist-privacy unlisted
```

The plan stores the exact `decision_events.id` authorizing every move. If the decision changes later, `watchlater-playlist show` marks that item stale and both executors refuse it.

## API quota planning

API plans default to the currently configured costs of 50 units per playlist creation and 50 units per playlist-item insertion, against a 10,000-unit allowance. They are planner inputs, not schema invariants:

```bash
watchlater-playlist plan \
    --backend api \
    --quota-limit 10000 \
    --playlist-create-cost 50 \
    --playlist-insert-cost 50
```

Browser plans have zero API quota estimate.

## Dry-run execution

Both backends are dry-run by default:

```bash
watchlater-playlist execute --run-id PLAN_ID
```

No OAuth client or Playwright browser is created unless `--apply` is present.

## API execution

Install API support and authorize a Google OAuth Desktop app:

```bash
pip install -e '.[youtube-api]'
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5
```

The API executor:

1. lists owned playlists again;
2. verifies planned stable playlist IDs;
3. resolves `create_planned` titles against the live account before creating anything;
4. refuses ambiguous live titles;
5. refuses to silently recreate a previously known destination ID that disappeared;
6. re-checks the current decision immediately before the live membership/write step;
7. checks live destination membership immediately before insertion;
8. treats a live duplicate as `already_present` success;
9. checkpoints creation, insertion and failure in SQLite;
10. updates the local inventory from confirmed live evidence.

See the root user guide for OAuth paths and options.

## Playwright execution

Install browser support and create the shared dedicated authenticated profile:

```bash
pip install -e '.[browser]'
playwright install chromium
watchlater-playlist browser-login
```

Then apply a browser plan cautiously:

```bash
watchlater-playlist execute --run-id BROWSER_PLAN_ID \
    --apply --max-writes 3
```

The Playwright backend uses the same checkpoint model as the API backend. It discovers exact playlist IDs, verifies exact `v=VIDEO_ID` membership, re-resolves missing destinations, confirms membership after Save actions, and fails rather than guessing when identities or titles are ambiguous. Headed mode is the default.

See [`playlist-browser.md`](playlist-browser.md) for browser-specific selectors, localization controls and identity checks.

## Write caps, resume and idempotence

`--max-writes` counts actual playlist creations plus video insertions. Read-only live checks do not count. Hitting the cap leaves the run `partial`; repeat the same command later to resume from persistent checkpoints.

Successful insertions are not repeated on resume. Failed items remain retriable. Planner-time `already_present` is rechecked live before becoming a completed checkpoint.

## Watch Later coordination

After an API or browser destination insertion succeeds (or live membership confirms the item is already present), build a separate removal plan:

```bash
watchlater-remove plan
watchlater-remove show
```

Only the exact current move decision with a confirmed destination checkpoint is eligible. Source removal remains an explicit Playwright operation under `watchlater-remove`; it never happens automatically as a side effect of playlist synchronization.

## Remaining issue #10 scope

Both execution backends and Watch Later coordination are implemented. The significant remaining requirement is modelling an explicit **one-video-to-multiple-destination-playlists** assignment. The current decision model still stores one `destination_playlist` on each current `move` decision.

Current quota references:

- <https://developers.google.com/youtube/v3/docs/playlists/insert>
- <https://developers.google.com/youtube/v3/docs/playlistItems/insert>
- <https://developers.google.com/youtube/v3/determine_quota_cost>
