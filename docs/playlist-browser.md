# Browser destination-playlist execution

The browser backend is the quota-free execution path for large destination-playlist moves. It consumes the same persisted `playlist_sync_*` plans/checkpoints as the YouTube Data API backend, so classification/planning state does not depend on the UI mechanism.

## Install and attach to a signed-in browser

Install the optional Playwright support:

```bash
pip install -e '.[browser]'
```

Google may refuse sign-in from a browser process launched under automation. The recommended flow therefore keeps **authentication outside Playwright**:

1. launch Chrome/Chromium yourself with a dedicated profile and a local DevTools endpoint;
2. sign in to YouTube manually in that normal browser window;
3. attach the executor to that already-authenticated browser with `--cdp-endpoint`.

For example on Linux:

```bash
google-chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.local/share/watchlater-chrome"
```

(`chromium` can be used instead of `google-chrome` where appropriate.) Do not use your ordinary live browser profile for remote debugging; keep a separate profile for this tool.

After signing in manually, you can verify the connection and open Watch Later:

```bash
watchlater-playlist browser-login \
    --cdp-endpoint http://127.0.0.1:9222
```

That command **attaches to the browser you already launched**; it does not launch a browser for Google login.

The older persistent-profile mode is still available when `--cdp-endpoint` is omitted, mainly for profiles that are already authenticated. New sign-ins should use the attached-browser flow above rather than attempting authentication in a Playwright-launched browser.

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
    --apply --max-writes 3 \
    --cdp-endpoint http://127.0.0.1:9222
```

With `--cdp-endpoint`, visibility/headless state is controlled by the browser you launched. In legacy Playwright-profile mode, headed mode remains the default and `--headless` is available only after validating your setup.

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
