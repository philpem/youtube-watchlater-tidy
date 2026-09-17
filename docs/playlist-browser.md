# Browser destination-playlist execution

The browser backend is the quota-free execution path for large destination-playlist moves. It consumes the same persisted `playlist_sync_*` plans/checkpoints as the YouTube Data API backend, so classification/planning state does not depend on the UI mechanism.

## Install and sign in

Install the optional Playwright support and Chromium:

```bash
pip install -e '.[browser]'
playwright install chromium
```

Use the same dedicated browser profile as selective Watch Later removal:

```bash
watchlater-playlist browser-login
```

The default profile is:

```text
.watchlater-playwright-profile/
```

It is ignored by Git. Sign in manually in the opened browser, then return to the terminal and press Enter.

Do not point this at your normal interactive Chrome/Chromium profile; use the dedicated automation profile.

## Plan browser-backed moves

Refresh/import normal-playlist inventory first, then create a browser plan:

```bash
watchlater-playlist inventory refresh
watchlater-playlist plan --backend browser
watchlater-playlist show
```

Browser plans use the same current `move` decisions and exact decision-event IDs as API plans, but their API quota estimate is zero.

## Dry-run first

```bash
watchlater-playlist execute --run-id PLAN_ID
```

Without `--apply` there is no browser creation and no checkpoint mutation.

## Apply a browser plan

Start with a small write cap:

```bash
watchlater-playlist execute --run-id PLAN_ID \
    --apply --max-writes 3
```

Headed mode is the default so UI actions remain visible. `--headless` is available only after validating your setup.

The executor also accepts pacing/retry controls:

```bash
watchlater-playlist execute --run-id PLAN_ID \
    --apply --max-writes 5 \
    --interval 3 --retries 2 --backoff 3
```

## Identity and safety checks

The Playwright client deliberately treats YouTube's DOM as untrusted/brittle UI state.

It:

1. discovers playlist IDs from exact `/playlist?list=...` URLs on YouTube's playlist page;
2. excludes special Watch Later/Liked Videos IDs from normal destination discovery;
3. refuses conflicting/ambiguous playlist identity/title information;
4. verifies a known destination by stable playlist ID before acting;
5. checks exact `v=VIDEO_ID` membership on the exact destination playlist before insertion;
6. for missing destinations, creates the playlist and then rediscovers exactly one live playlist with that title before treating creation as successful;
7. navigates to the exact `watch?v=VIDEO_ID` page for insertion and verifies the page identity before touching the Save dialog;
8. selects exactly one destination title in the Save dialog;
9. verifies the video ID again immediately before the save action;
10. does not consider the click successful until exact destination membership is visible afterward.

If these checks cannot establish identity safely, execution fails/checkpoints the item instead of guessing.

Planner-time `already_present` inventory is still rechecked live; only live membership evidence is a completed browser checkpoint.

## Localization / UI changes

Default UI labels are English:

```text
Save
New playlist
Create
```

Override them when required:

```bash
watchlater-playlist execute --run-id PLAN_ID --apply \
    --save-label 'Save' \
    --new-playlist-label 'New playlist' \
    --create-label 'Create'
```

You can also choose a Playwright Chromium channel and profile explicitly:

```bash
watchlater-playlist execute --run-id PLAN_ID --apply \
    --channel chrome \
    --user-data-dir ~/.local/share/watchlater-playwright
```

DOM/text changes may still require code updates; the client intentionally fails when its identity/menu assumptions do not match the current UI.

## Playlist creation privacy

Plans retain their requested privacy (`private`, `unlisted`, or `public`). The browser adapter assumes YouTube's normal private default for `private`; for non-private creation it requires an identifiable privacy combobox and exact matching option. If the current UI does not expose that safely, it refuses rather than silently creating the wrong privacy.

## Checkpoint/resume semantics

`execute_browser_plan()` continues to provide the execution-neutral safety layer:

- stale decision-event IDs are refused;
- the current move decision is rechecked immediately before live membership/write operations;
- duplicate live membership becomes `already_present` success;
- creation, insertion and failures are checkpointed in SQLite;
- successful browser evidence updates the local playlist inventory;
- `failed`/unfinished operations remain retriable;
- successful operations are not repeated on resume;
- `--max-writes` counts actual playlist creations plus item insertions only.

## Interaction with Watch Later removal

A confirmed browser `inserted` or live-verified `already_present` checkpoint for the exact move decision satisfies the move gate used by `watchlater-remove plan`. Watch Later removal remains a separate explicit operation and never happens automatically as a side effect of destination insertion.
