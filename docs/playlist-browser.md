# Browser destination-playlist execution

Issue #10 supports the same persisted playlist plan through either the official YouTube Data API or an authenticated Playwright browser. The browser backend exists for quota-heavy moves where API insertion cost is unattractive.

The safety/checkpoint semantics live in `playlist_browser.py`; the YouTube UI adapter lives in `playlist_playwright.py`.

## Install and sign in

Install the optional browser extra and Chromium:

```bash
pip install -e '.[browser]'
playwright install chromium
```

The destination-playlist executor reuses the same dedicated persistent profile as selective Watch Later removal:

```text
.watchlater-playwright-profile/
```

Use the existing login helper once:

```bash
watchlater-remove login
```

Do not point automation at your normal interactive browser profile.

## Plan with the browser backend

```bash
watchlater-playlist plan --backend browser
watchlater-playlist show
```

The planner uses the same current `move` decisions and inventory as the API backend. Browser plans have zero API quota cost.

## Dry-run first

```bash
watchlater-playlist execute --run-id PLAN_ID
```

Without `--apply`, no Playwright context is created and no checkpoint is changed.

## Apply through Playwright

```bash
watchlater-playlist execute --run-id PLAN_ID --apply --max-writes 5
```

Headed mode is the default. `--headless` should only be used after validating a headed run.

Useful pacing/retry controls:

```bash
watchlater-playlist execute --run-id PLAN_ID --apply \
    --max-writes 5 \
    --interval 3 \
    --retries 2 \
    --backoff 3
```

## Live identity checks

The adapter fails closed when it cannot prove the intended destination/video identity.

### Playlist discovery

Owned/saved playlists are discovered from:

```text
https://www.youtube.com/feed/playlists
```

The adapter extracts stable `list=PLAYLIST_ID` values from playlist links and scrolls until the page is stable. An incomplete scan at `--max-scrolls` is an error rather than permission to assume a missing destination.

Special/system playlists such as Watch Later and mixes are excluded from normal destinations.

### Existing membership

Membership checks navigate directly to:

```text
https://www.youtube.com/playlist?list=PLAYLIST_ID
```

Rows are matched by exact `v=VIDEO_ID`, not playlist order or title text. If the playlist does not reach a stable end before the scroll limit, absence is not assumed.

### Playlist creation

YouTube currently documents desktop playlist creation in YouTube Studio as **Create -> New playlist**. The adapter follows that flow, sets the requested privacy, saves, then re-discovers the new playlist by stable ID/title before returning success.

A title that cannot be uniquely rediscovered is treated as an error.

### Adding a video

The adapter opens the exact destination playlist page and uses its **Add** flow. It searches using the full watch URL, requires the result to contain the exact requested video ID, and checks that the current page still has the expected `list=PLAYLIST_ID` immediately before the final **Save to playlist** action.

After the UI write it reloads the exact destination playlist and confirms exact membership. A UI click without confirmed destination membership is not checkpointed as success.

## Localization / UI changes

YouTube DOM and visible labels are not a stable API. The default English labels can be overridden:

```bash
watchlater-playlist execute --run-id PLAN_ID --apply \
    --create-label 'Create' \
    --new-playlist-label 'New playlist' \
    --add-label 'Add' \
    --save-to-playlist-label 'Save to playlist' \
    --studio-save-label 'Save' \
    --visibility-label 'Visibility' \
    --search-label 'Search'
```

If YouTube changes the UI enough that those controls or exact identities cannot be established, execution fails rather than choosing a nearby control heuristically.

## Shared executor safety

The browser executor:

1. refuses plans not created with `backend=browser`;
2. refuses stale decision-event IDs before launching a write run;
3. rechecks the current decision immediately before live membership/write operations;
4. resolves `create_planned` destination names against the live account before creation;
5. refuses ambiguous live destination titles;
6. refuses to silently recreate a destination whose stable planned playlist ID disappeared;
7. live-rechecks planner-time `already_present` rows;
8. treats confirmed live duplicates as `already_present` success;
9. checkpoints playlist creation, insertion, duplicate membership and failure in `playlist_sync_*`;
10. updates the local inventory from confirmed browser evidence;
11. supports retry/backoff, pacing and `--max-writes` partial execution;
12. resumes without repeating confirmed work.

A browser-confirmed `inserted` or live-rechecked `already_present` row is sufficient evidence for the later Watch Later removal planner to allow the corresponding `move` item.

## Current limitations

- Browser discovery can include playlists saved to the account as well as owned playlists. A non-writable destination will fail safely during creation/insertion rather than being silently replaced.
- The implementation depends on YouTube's current web UI and visible labels; it should be expected to need maintenance when YouTube changes the DOM.
- One current `move` decision still targets one destination playlist. Explicit multi-destination moves remain separate work.

Relevant YouTube help pages:

- <https://support.google.com/youtube/answer/57792>
- <https://support.google.com/youtube/answer/10232933>
- <https://support.google.com/youtube/answer/6109639>
