# Selective Watch Later removal

Issue #7 uses two deliberately separate layers:

1. a local removal plan/checkpoint layer;
2. a Playwright executor that applies an already-reviewed plan by exact video ID.

The planner never changes YouTube. Browser execution is dry-run by default and requires two
explicit destructive flags before it can click `Remove from Watch later`.

## Build a removal plan

```bash
watchlater-remove plan
```

or target a particular imported snapshot:

```bash
watchlater-remove plan --snapshot 2 --output removal-plan.json
```

The planner considers only current local decisions with actions:

- `delete`;
- `archive`;
- `move` **after its destination has been confirmed for the exact same decision event**.

`keep` and `review` are never removal candidates.

## Move safety gate

A `move` means "add to the destination first, then remove from Watch Later". Therefore a
move is excluded from the removal plan unless a playlist-sync checkpoint exists for the
same:

- video ID;
- `decision_events.id`;
- destination playlist name.

Accepted destination checkpoints are:

- `inserted`; or
- `already_present` **after a live executor re-check**, represented by `attempted_at` being
  non-null.

An `already_present` value inherited only from an old playlist inventory is deliberately
not enough to authorize source removal.

Blocked moves are shown in the plan output with a reason, but are not inserted into the
executable removal-item set.

## Stale plans

Every removal item stores the exact decision-event ID that authorized it. Inspect a stored
plan with:

```bash
watchlater-remove show
watchlater-remove show --run-id 3
```

If the current action/destination has changed since planning, the item is reported as:

```json
"stale": true
```

The executor refuses a plan containing stale items before opening a destructive run, and it
checks authorization again immediately before each browser attempt.

## Install Playwright support

The browser executor is optional:

```bash
pip install -e '.[browser]'
playwright install chromium
```

The default persistent profile is:

```text
.watchlater-playwright-profile/
```

It is ignored by Git. Use this dedicated automation profile rather than your normal browser
profile.

Open it interactively to sign in to YouTube once:

```bash
watchlater-remove login
```

The command opens Watch Later in a persistent Chromium context. Sign in manually if needed,
then return to the terminal and press Enter to close the browser. Subsequent executor runs
reuse the same profile.

Use `--user-data-dir PATH` to choose another profile directory. `--channel` can select a
Playwright-installed browser channel when required.

## Dry-run execution

Inspect what the executor would process without starting Playwright:

```bash
watchlater-remove execute --run-id 3
```

Without `--apply`, there is no browser creation and no checkpoint mutation.

## Destructive execution

A real run requires **both** flags:

```bash
watchlater-remove execute --run-id 3 \
    --apply \
    --confirm-remove
```

`--apply` alone is rejected. This is intentional because Watch Later removal cannot be
undone reliably from the tool.

Start with a small cap:

```bash
watchlater-remove execute --run-id 3 \
    --apply --confirm-remove \
    --max-deletes 3
```

Completed `removed` / `already_absent` rows are terminal checkpoints and are skipped on a
later resume. `not_found` and `failed` remain retriable.

## Exact-ID browser behavior

The browser adapter does not trust playlist order. For each planned video it:

1. opens the Watch Later playlist;
2. progressively scrolls until an exact `v=VIDEO_ID` row is found or the loaded playlist is
   stable;
3. parses the row's watch URL and verifies the exact video ID;
4. opens that row's action menu;
5. verifies the exact row identity again immediately before the destructive click;
6. clicks the configured `Remove from Watch later` menu item;
7. confirms the exact row disappeared before returning `removed`.

If the page reaches a stable complete scan without that exact ID, the result is
`already_absent`. If the configured scroll limit is reached first, the result is `not_found`
and remains retriable.

YouTube's DOM and UI text are not a stable API. Useful compatibility options include:

```bash
watchlater-remove execute --run-id 3 --apply --confirm-remove \
    --action-menu-label 'Action menu' \
    --remove-label 'Remove from Watch later' \
    --max-scrolls 250 \
    --scroll-pause 0.7
```

For another YouTube language, pass the actual localized remove-menu text with
`--remove-label` rather than allowing the tool to guess a destructive action.

## Pacing and retries

Defaults are conservative:

```text
interval between confirmed removals: 2 seconds
retries after not-found/browser failure: 1
exponential retry backoff base: 2 seconds
maximum removals per invocation: 10
```

Override them explicitly when needed:

```bash
watchlater-remove execute --run-id 3 --apply --confirm-remove \
    --max-deletes 5 \
    --interval 3 \
    --retries 2 \
    --backoff 3
```

A small `--max-deletes` is recommended for initial real-world testing because YouTube UI
changes can break selectors at any time.

## Checkpoint states

The removal table distinguishes:

- `planned` — not attempted yet;
- `removed` — confirmed removed from Watch Later;
- `already_absent` — exact video absent after a stable full scan;
- `not_found` — presence/absence could not be established within the configured scan;
- `failed` — browser attempt raised/final verification failed;
- `skipped` — intentionally deferred.

`removed` and `already_absent` are terminal success states. `not_found`, `failed`, and
`skipped` are eligible for later retry. The imported catalogue/snapshot remains intact after
successful removal.

## Headless mode

Interactive/headed execution is the default because it makes destructive behavior visible.
For established automation, `--headless` is available:

```bash
watchlater-remove execute --run-id 3 --apply --confirm-remove --headless
```

Use headless mode only after validating the selectors and profile interactively.

## Scope

This executor removes exact planned IDs from **Watch Later only**. Normal destination
playlist creation/insertion is handled separately by `watchlater-playlist`. Browser-backed
bulk destination-playlist insertion remains separate work; it is not conflated with source
removal.
