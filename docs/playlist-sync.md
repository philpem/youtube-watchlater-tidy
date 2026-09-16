# Destination playlist synchronization

Playlist synchronization deliberately separates **classification**, **planning**, and **execution**. A stored LLM suggestion is never executable merely because a model proposed `move`; only a current local `move` decision is allowed into a plan.

The current implementation supports:

1. destination-playlist inventory from versioned JSON or the authenticated YouTube Data API;
2. persistent/idempotent move planning;
3. quota and stale-decision checks;
4. authenticated YouTube Data API execution for normal playlists;
5. resumable per-playlist/per-video checkpoints.

Watch Later removal is still separate work under issue #7. A successful destination insertion does **not** itself remove the source item from Watch Later.

## Install API support

The normal package stays lightweight. Install Google API/OAuth support explicitly:

```bash
pip install -e '.[youtube-api]'
```

This adds Google's maintained Python client/auth packages (`google-api-python-client`, `google-auth-oauthlib`, and `google-auth-httplib2`).

## OAuth setup

Create an OAuth **Desktop app** client in a Google Cloud project with the YouTube Data API enabled, then download the client-secret JSON.

The CLI defaults to:

```text
client_secret.json
.youtube-watchlater-token.json
```

Both default names are ignored by Git. The token cache is written mode `0600` where the platform permits it.

On first authenticated use, the installed-app OAuth flow opens a local browser authorization page. Later runs reuse/refresh the token cache.

Override either path when needed:

```bash
watchlater-playlist inventory refresh \
    --client-secrets ~/private/youtube-client.json \
    --token ~/private/youtube-token.json
```

The requested OAuth scope is `https://www.googleapis.com/auth/youtube.force-ssl`, which is sufficient for normal playlist reads/writes used here.

## Refresh the live playlist inventory

Before planning, refresh owned normal playlists and their known membership:

```bash
watchlater-playlist inventory refresh
```

The command lists owned playlists and then their items, showing a progress bar. Disable it with `--no-progress`.

Inspect the local cache:

```bash
watchlater-playlist inventory show
```

The same versioned inventory can still be imported manually:

```bash
watchlater-playlist inventory import playlist-inventory.json
```

The format remains:

```text
youtube-watchlater-tidy-playlist-inventory-v1
```

See [`examples/playlist-inventory.example.json`](../examples/playlist-inventory.example.json).

Duplicate playlist IDs and duplicate video IDs inside one playlist are rejected. Duplicate **titles** are retained because YouTube permits them; planning refuses an ambiguous title rather than guessing.

## Build a move plan

Only current catalogue decisions where:

```text
action = move
```

are included. LLM `move` proposals remain advisory until a human/rule workflow turns them into a current decision.

Create a plan:

```bash
watchlater-playlist plan --backend api
```

For every destination:

- exactly one inventory title match -> stable existing playlist ID;
- no match -> `create_planned`;
- more than one title match -> planning fails as ambiguous.

New destinations default to private; choose another privacy explicitly if required:

```bash
watchlater-playlist plan --backend api --new-playlist-privacy unlisted
```

The plan stores the exact `decision_events.id` that authorized every move. If the decision changes later, `watchlater-playlist show` marks that item stale.

```bash
watchlater-playlist show
watchlater-playlist show --run-id 4
```

## API quota planning

The planner defaults currently reflect Google's documented write costs:

- `playlists.insert`: 50 units;
- `playlistItems.insert`: 50 units;
- default combined allowance used by this tool: 10,000 units.

These are configurable planner inputs, not schema invariants:

```bash
watchlater-playlist plan \
    --backend api \
    --quota-limit 10000 \
    --playlist-create-cost 50 \
    --playlist-insert-cost 50
```

The estimate is:

```text
new destination playlists × create cost
+ missing destination memberships × insert cost
```

A plan exceeding its configured allowance is still persisted for inspection but exits non-zero unless `--allow-over-quota` is supplied. Execution independently refuses an over-quota plan unless explicitly overridden again.

## Dry-run execution is the default

Inspect what would execute without OAuth/network writes:

```bash
watchlater-playlist execute --run-id 4
```

No authenticated client is created unless `--apply` is present.

The executor refuses an API plan before writes when:

- any planned item is stale relative to its authorizing decision event;
- the stored plan exceeds its configured quota allowance and `--allow-over-quota` is absent;
- the run was planned for the browser backend rather than API.

## Apply an API plan

After reviewing the stored plan:

```bash
watchlater-playlist execute --run-id 4 --apply
```

Use non-default OAuth files if needed:

```bash
watchlater-playlist execute --run-id 4 --apply \
    --client-secrets ~/private/youtube-client.json \
    --token ~/private/youtube-token.json
```

### Live safety checks

At execution time the API backend:

1. lists owned playlists again;
2. uses a planned stable playlist ID when one already existed;
3. for a `create_planned` destination, resolves the title against the **live** account before creating anything;
4. refuses ambiguous live titles;
5. refuses to silently recreate a previously known destination ID that has disappeared;
6. re-checks the current decision-event identity before the live membership/write step;
7. checks live destination membership immediately before every planned insertion;
8. treats an already-present video as success rather than inserting a duplicate;
9. checkpoints successful creation/insertion or failure in SQLite.

A newly created destination is also written into the local playlist inventory. Successful/already-present item evidence is cached there as well, making later planning useful even before another full inventory refresh.

## Limit writes per invocation

For conservative rollout or testing:

```bash
watchlater-playlist execute --run-id 4 --apply --max-writes 5
```

`--max-writes` counts actual destination-playlist creations plus item insertions. Read-only live checks do not count. Hitting the cap leaves the run `partial`; repeat the command to resume from persistent checkpoints.

## Resume and idempotence

The executor works from `playlist_sync_destinations` and `playlist_sync_items`, not playlist order. Successful insertions are not repeated on resume. Failed items remain retryable. A live duplicate found before insertion is checkpointed as `already_present`.

Example:

```bash
watchlater-playlist execute --run-id 4 --apply --max-writes 2
watchlater-playlist show --run-id 4
watchlater-playlist execute --run-id 4 --apply
```

If a current move decision changes between invocations, create a new plan rather than trying to force the stale one.

## Inventory JSON remains useful

Manual/offline inventories remain supported for testing, auditing, and the future browser executor. The planner and checkpoint tables are execution-neutral; an API plan and browser plan represent the same local move intent even though their write mechanisms differ.

## What remains for issue #10

The official API backend now handles normal playlist creation/insertion. Remaining issue #10 work is primarily:

1. authenticated browser executor for quota-heavy bulk playlist work;
2. tighter coordination with issue #7 so Watch Later removal occurs only after a `move` destination is confirmed `inserted` or `already_present`;
3. any future support for one video intentionally targeting multiple playlists.

Current quota references:

- <https://developers.google.com/youtube/v3/docs/playlists/insert>
- <https://developers.google.com/youtube/v3/docs/playlistItems/insert>
- <https://developers.google.com/youtube/v3/determine_quota_cost>
