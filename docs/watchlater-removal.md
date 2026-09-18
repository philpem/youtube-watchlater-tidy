# Selective Watch Later removal

Watch Later cleanup deliberately separates planning from execution:

1. a local removal plan/checkpoint layer;
2. a Playwright executor that applies an already-reviewed plan by exact video ID.

The planner never changes YouTube. Browser execution is dry-run by default and requires two explicit destructive flags before it can click `Remove from Watch later`.

## Build a removal plan

### Removal-only workflow

If every video you intend to remove has an `archive` or `delete` decision, no normal
destination playlist and no playlist inventory are required. Start here, not with
`watchlater-playlist plan`:

```bash
watchlater-remove plan
```

or target a particular imported snapshot:

```bash
watchlater-remove plan --snapshot 2 --output removal-plan.json
```

The planner considers only current decisions with actions:

- `delete`;
- `archive`;
- `move`, but only after all required destination playlists have been confirmed for the exact same decision event.

`keep` and `review` are never removal candidates.

## Move safety gate

A `move` means "add to destination playlist(s) first, then remove from Watch Later".

For a single-destination move, the planner requires a playlist-sync checkpoint matching the same:

- video ID;
- `decision_events.id`;
- destination playlist name.

For a multi-destination move, **every destination on that decision event must be confirmed independently** before the video is eligible for Watch Later removal.

Accepted destination checkpoints are:

- `inserted`; or
- `already_present` after a live executor re-check, represented by `attempted_at` being non-null.

An `already_present` value inherited only from an old playlist inventory is deliberately not enough.

If one or more destinations are still missing, the move is excluded from the executable removal set. The plan JSON reports the complete destination set plus `missing_destinations`, so it is clear why the item remains blocked.

Changing the current move decision supersedes the destination set as a whole; an old playlist-sync result from a previous decision event cannot authorize removal.

## Stale plans

Every removal item stores the exact decision-event ID that authorized it. Inspect a stored plan with:

```bash
watchlater-remove show
watchlater-remove show --run-id 3
```

If the current decision changes since planning, the item is reported with:

```json
"stale": true
```

The executor refuses a plan containing stale items before opening a destructive run and rechecks authorization immediately before each browser attempt.

## Install Playwright support and authenticate outside automation

Install the optional browser support:

```bash
pip install -e '.[browser]'
```

Google may reject login attempts from a Playwright-launched browser. The recommended Watch Later flow therefore attaches to a browser that you launch and authenticate manually.

For example on Linux:

```bash
google-chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.local/share/watchlater-chrome"
```

Use `chromium` instead where appropriate. Sign in to YouTube manually in that browser window. Keep this as a dedicated profile rather than enabling remote debugging on your normal interactive profile.

You can then verify/prepare the session with either command:

```bash
watchlater-remove login --cdp-endpoint http://127.0.0.1:9222
# or
watchlater-playlist browser-login --cdp-endpoint http://127.0.0.1:9222
```

These commands attach to the already-running browser and navigate to Watch Later; they do not launch a browser for Google authentication.

The older `.watchlater-playwright-profile/` launch mode remains available when `--cdp-endpoint` is omitted, for already-authenticated profiles. Do not rely on that mode for a fresh Google login.

## Dry-run execution

```bash
watchlater-remove execute --run-id 3
```

Without `--apply`, there is no browser creation and no checkpoint mutation.

`execute` prints one concise result line after the live progress messages. To emit the
complete execution object and stored plan for a script or detailed inspection, add `--json`:

```bash
watchlater-remove execute --run-id 3 --json
```

`watchlater-remove show --run-id 3` remains the focused command for inspecting the
stored plan and per-video checkpoints.

## Destructive execution

A real run requires both flags:

```bash
watchlater-remove execute --run-id 3 \
    --apply \
    --confirm-remove \
    --cdp-endpoint http://127.0.0.1:9222
```

`--apply` alone is rejected. Start with a small cap:

```bash
watchlater-remove execute --run-id 3 \
    --apply --confirm-remove \
    --max-deletes 3 \
    --cdp-endpoint http://127.0.0.1:9222
```

Completed `removed` / `already_absent` rows are terminal checkpoints and are skipped on resume. `not_found` and `failed` remain retriable.

## Exact-ID browser behavior

The browser adapter makes one forward pass through Watch Later for all pending plan items:

1. it opens Watch Later at the top and reads the exact video IDs from all currently loaded rows;
2. it removes every loaded row whose ID is in the reviewed removal plan;
3. before each click, it rechecks the exact current decision and verifies the row ID again;
4. it checkpoints each confirmed removal immediately;
5. only when no loaded row matches does it scroll to request more rows;
6. it stops when the playlist content is stable at the actual bottom.

This avoids rescanning a large playlist once per planned video. Page-height jitter is not
used as the end-of-list signal; the scanner uses bottom position plus stable loaded video
identity. Progress is printed to stderr while the scan runs.

After a stable complete scan, planned IDs never encountered are checkpointed
`already_absent`. If the configured scroll limit is reached first, unresolved IDs become
`not_found` and remain retriable.

Useful localization/UI options:

```bash
watchlater-remove execute --run-id 3 --apply --confirm-remove \
    --action-menu-label 'Action menu' \
    --remove-label 'Remove from Watch later' \
    --max-scrolls 250 \
    --scroll-pause 0.7
```

For another YouTube language, pass the actual localized destructive text rather than allowing the tool to guess.

## Pacing and retries

Defaults are conservative:

```text
interval between confirmed removals: 2 seconds
retries after not-found/browser failure: 1
exponential retry backoff base: 2 seconds
maximum removals per invocation: 10
```

Override explicitly when needed:

```bash
watchlater-remove execute --run-id 3 --apply --confirm-remove \
    --max-deletes 5 \
    --interval 3 \
    --retries 2 \
    --backoff 3
```

A small `--max-deletes` is recommended for initial real-world testing because YouTube UI changes can break selectors at any time.

## Checkpoint states

- `planned` — not attempted yet;
- `removed` — confirmed removed from Watch Later;
- `already_absent` — exact video absent after a stable full scan;
- `not_found` — presence/absence could not be established within the configured scan;
- `failed` — browser attempt raised/final verification failed;
- `skipped` — intentionally deferred.

`removed` and `already_absent` are terminal success states. `not_found`, `failed`, and `skipped` are eligible for later retry. The imported catalogue/snapshot remains intact after successful removal.

It is safe to interrupt with Ctrl-C. Confirmed rows are checkpointed immediately; rerunning
the same plan resumes the remaining items. If interruption happens after a YouTube click but
before its checkpoint, the next complete scan reconciles that exact ID as already absent.

## Headless mode

In attached-browser mode, headed/headless state is controlled by the browser you launched. In legacy Playwright-profile mode, headed execution is the default because it keeps destructive behavior visible. For an already-validated legacy setup:

```bash
watchlater-remove execute --run-id 3 --apply --confirm-remove --headless
```

## Scope

This executor removes exact planned IDs from Watch Later only. Normal destination playlist creation/insertion is handled by `watchlater-playlist` using either the API or Playwright backend. Those destination executors may confirm one or many playlist targets for the same move decision; Watch Later removal remains a separate explicit step and waits until all requested destinations are confirmed.
