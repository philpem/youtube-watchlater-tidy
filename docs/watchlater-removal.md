# Selective Watch Later removal

Watch Later removal is the destructive execution stage for local `delete`, `archive` and
successfully-moved videos. It uses an authenticated Playwright browser because the official
YouTube Data API does not expose Watch Later as a normal accessible playlist.

The executor works from **persisted local decisions and exact video IDs**. It never derives
what to remove from the current Watch Later order.

## Install the optional browser support

```bash
pip install -e '.[browser]'
playwright install chromium
```

A dedicated persistent browser profile is used by default:

```text
.watchlater-playwright-profile/
```

It contains login/session state and is gitignored.

Open it once for manual YouTube login:

```bash
watchlater-remove login
```

You may use an installed Chrome/Chromium channel with `--channel` when appropriate.

## What is eligible for removal

Create a plan:

```bash
watchlater-remove plan --output watchlater-removal-plan.json
```

The planner considers only **current** local decisions:

- `delete` -> eligible;
- `archive` -> eligible;
- `move` -> eligible only after the same `decision_events.id` has a confirmed destination
  playlist checkpoint;
- `keep` / `review` -> never eligible.

For a `move`, confirmation means the destination sync item is either:

```text
inserted
```

or a live-verified:

```text
already_present
```

An `already_present` value copied only from an old planning inventory is not sufficient.
This prevents removal from Watch Later before destination preservation has actually been
confirmed.

Blocked moves are listed in the plan rather than silently dropped.

## Stale-plan protection

Every removal item stores the exact decision-event ID that authorized it. Before execution,
and again before each browser attempt, the tool verifies that the same decision remains
current.

For `move`, it also verifies that the same move decision still has a confirmed destination
checkpoint. Changing the action or destination makes the old removal plan stale.

Inspect a plan with:

```bash
watchlater-remove show
watchlater-remove show --run-id 3
```

## Dry-run is the default

This command launches no browser and performs no removal:

```bash
watchlater-remove execute --run-id 3
```

Actual removal requires **both** destructive flags:

```bash
watchlater-remove execute --run-id 3 \
    --apply \
    --confirm-remove
```

The default destructive cap is 10 successful removals per invocation. Start smaller when
first testing against the live YouTube UI:

```bash
watchlater-remove execute --run-id 3 \
    --apply --confirm-remove \
    --max-deletes 2
```

Successfully completed items are checkpointed and skipped on resume.

## Browser identity checks

For each video ID the Playwright adapter:

1. opens the Watch Later playlist using the persistent authenticated profile;
2. scans/scrolls playlist rows looking for an href whose parsed `v=` value exactly matches
   the planned video ID;
3. scrolls that exact row into view;
4. verifies the exact `v=` ID immediately before opening the action menu;
5. opens that row's action menu;
6. verifies the exact row ID again immediately before clicking `Remove from Watch later`;
7. confirms the exact row disappeared.

It never treats playlist position as identity.

YouTube's DOM and localized strings are not stable APIs. The relevant labels are therefore
configurable:

```bash
watchlater-remove execute --run-id 3 \
    --apply --confirm-remove \
    --action-menu-label 'Action menu' \
    --remove-label 'Remove from Watch later'
```

Use labels matching the YouTube language in the persistent profile.

## Result states

Every attempt is checkpointed as one of:

- `removed` - exact row found, remove command clicked and disappearance confirmed;
- `already_absent` - a stable full Watch Later scan completed without that exact ID;
- `not_found` - the configured scroll bound was reached before a stable full scan;
- `failed` - browser/DOM/menu/identity verification failed;
- `skipped` - reserved for controlled executor skips.

`removed` and `already_absent` are terminal. `not_found` and `failed` are retried by later
runs.

A stable full scan can only describe what the current YouTube UI exposes. If unavailable
items are hidden by YouTube, enable `Show unavailable videos` in the Watch Later UI before
trying to remove those placeholders.

## Pacing, retry and bounds

Defaults are deliberately conservative:

```text
max successful removals: 10 / invocation
interval after each removal: 2 seconds
retries: 1
retry backoff: 2 seconds, exponential
maximum scroll rounds per lookup: 250
scroll pause: 0.7 seconds
```

Tune them explicitly if needed:

```bash
watchlater-remove execute --run-id 3 \
    --apply --confirm-remove \
    --max-deletes 5 \
    --interval 3 \
    --retries 2 \
    --backoff 3 \
    --max-scrolls 400
```

## Authentication/profile safety

The tool does not automate Google login. Use `watchlater-remove login` and authenticate
interactively in the dedicated persistent profile. Do not commit or share that profile.

Headless execution is available after the profile is already authenticated:

```bash
watchlater-remove execute --run-id 3 \
    --apply --confirm-remove --headless
```

For initial testing, headed mode is preferable so the exact UI operations remain visible.

## Relationship to playlist synchronization

Destination playlist synchronization (#10) and Watch Later removal (#7) intentionally use
separate executors and checkpoint tables.

For a `move` action the required ordering is:

```text
reviewed move decision
        -> destination inserted / live-confirmed already present
        -> Watch Later removal becomes eligible
```

A deletion/archive action does not require destination synchronization. No browser removal
ever rewrites or deletes the imported catalogue snapshot.
