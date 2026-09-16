# Watch Later selective removal planning

Issue #7 is split into a local planning/checkpoint layer and a later Playwright executor.
This document describes the **implemented planning layer only**. Nothing here clicks or
changes YouTube.

## Build a plan

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

The later browser executor must refuse stale items.

## Checkpoint states

The local schema already defines the states needed for resumable browser execution:

- `planned` — not attempted yet;
- `removed` — confirmed removed from Watch Later;
- `already_absent` — exact video already absent when checked;
- `not_found` — current UI scan could not establish presence/absence; retriable;
- `failed` — attempted but failed; retriable;
- `skipped` — intentionally deferred; retriable.

`removed` and `already_absent` are terminal success states. The imported catalogue/snapshot
is never deleted or rewritten when a removal eventually succeeds.

## What is not implemented in this tranche

There is deliberately no Playwright dependency or browser command in this PR. The next
tranche will add:

- persistent authenticated browser profile;
- exact-video-ID lookup in Watch Later;
- identity re-check immediately before clicking `Remove from Watch later`;
- dry-run by default plus explicit destructive confirmation;
- pacing/backoff and per-invocation delete cap;
- checkpoint/resume using the tables defined here.
