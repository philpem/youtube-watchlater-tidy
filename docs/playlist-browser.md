# Browser destination-playlist executor foundation

This is the execution-neutral browser foundation for issue #10. It deliberately contains **no Playwright selectors yet** and cannot perform real browser writes from the CLI in this tranche.

The purpose is to make the safety/checkpoint semantics independently testable before coupling them to YouTube's web UI.

## Planning

Create the same local move plan using the browser backend:

```bash
watchlater-playlist plan --backend browser
watchlater-playlist show
```

The planner uses the same current `move` decisions and destination inventory as the API backend. Browser plans report zero **API** quota cost.

## Dry-run execution

The normal `execute` command now understands browser plans:

```bash
watchlater-playlist execute --run-id PLAN_ID
```

This performs no browser/network access and no checkpoint mutation. `--apply` deliberately refuses until the Playwright adapter is added in the next tranche.

## Executor contract

`playlist_browser.execute_browser_plan()` consumes a `PlaylistBrowserClient` with four operations:

- list live destination playlists;
- find an exact video ID in an exact destination playlist;
- create one destination playlist;
- insert one exact video ID into one exact destination playlist.

The later Playwright adapter implements that protocol. Unit tests use a fake browser implementation now.

## Safety semantics already implemented

The browser executor foundation:

1. refuses plans not created with `backend=browser`;
2. refuses stale decision-event IDs before writes;
3. rechecks the current decision immediately before the live membership/write step;
4. resolves `create_planned` destination names against the live account before creating anything;
5. refuses ambiguous live destination titles;
6. refuses to silently recreate a destination whose stable planned playlist ID disappeared;
7. rechecks destination membership immediately before insertion;
8. treats a live duplicate as `already_present` success;
9. revalidates planner-time `already_present` rows rather than trusting old inventory;
10. checkpoints playlist creation, insertion, duplicate membership and failure in the existing `playlist_sync_*` tables;
11. supports retry/backoff, pacing and a maximum write count;
12. resumes without repeating confirmed work.

Successful browser evidence also refreshes the corresponding rows in the local playlist inventory.

## Write cap and resume

The generic executor supports the same conservative concept as the API backend:

```text
max_writes = playlist creations + video insertions
```

Read-only live checks do not consume the cap. A capped run remains `partial` and can be resumed without recreating a successful destination or reinserting a confirmed video.

## What remains for the next tranche

The next tranche adds the real Playwright implementation of `PlaylistBrowserClient`, including:

- a dedicated authenticated browser profile;
- exact playlist identity resolution;
- exact video identity verification;
- UI actions for playlist creation and insertion;
- headed-by-default execution;
- configurable localization/selectors and diagnostic screenshots/HTML on UI failures.

Until that lands, `watchlater-playlist execute --run-id BROWSER_PLAN --apply` intentionally refuses.
