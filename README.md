# youtube-watchlater-tidy

Tools for exporting, triaging, classifying, and cleaning up a large YouTube Watch Later playlist.

## Export Watch Later metadata

`yt-dlp` can export the playlist metadata using your logged-in browser session without downloading the videos themselves:

```bash
yt-dlp \
    --cookies-from-browser firefox \
    --flat-playlist \
    --dump-single-json \
    'https://www.youtube.com/playlist?list=WL' \
    > watch-later.json
```

Change `firefox` to the browser/profile source you use if necessary.

Keep the resulting JSON as a backup before deleting anything from YouTube.

`--flat-playlist` deliberately avoids extracting every video individually, so some entry metadata can be absent. If a creator report contains a large `(unknown)` cohort, inspect the individual entries with:

```bash
watchlater videos --unknown-creator --remaining
```

For YouTube in particular, make sure the export was produced with a recent yt-dlp: a flat-playlist channel/uploader metadata regression was fixed upstream in June 2026. Re-exporting with a current build may recover missing creator metadata without doing slower per-video enrichment.

### Repair missing live-video metadata

If current yt-dlp still leaves creator metadata blank for otherwise-live videos, selectively run full extraction only for those entries:

```bash
watchlater enrich --missing-creator --dry-run
watchlater enrich --missing-creator
```

This invokes `yt-dlp --skip-download --dump-single-json` for the selected videos. The command reports the candidate count immediately and shows a `tqdm` progress bar with the current video and running found/failed totals. Use `--no-progress` to suppress the progress bar.

The original imported snapshot is not changed. Richer metadata is stored as a separate observation with provenance and is then used by creator/video reports and creator cohort selection where the snapshot has gaps.

Successful yt-dlp observations are cached. Use `--refresh` to fetch them again, `--limit N` to cap a run, or target one known video explicitly:

```bash
watchlater enrich --missing-creator --limit 10
watchlater enrich --video-id zquMVVCnmuk
watchlater enrich --video-id zquMVVCnmuk --refresh
```

Private/deleted markers are excluded from `--missing-creator`.

### Recover deleted/private videos from public archives

The surviving YouTube video ID can be checked against the FindYouTubeVideo v5 service, which federates several archive/index sources. Start with a dry run if desired:

```bash
watchlater recover --unavailable --dry-run
watchlater recover --unavailable
```

Recovery uses exact video IDs only. Found and not-found results are cached so repeated runs do not re-query the service unnecessarily; use `--refresh` to force a new lookup. Long batches show a `tqdm` progress bar and can be capped with `--limit`:

```bash
watchlater recover --unavailable --limit 10
watchlater recover --video-id NTY0d9KM0Hw
```

The federated response is preserved verbatim in the local catalogue together with its verdict and archive links. Inspect a cached result without making another network request using:

```bash
watchlater recovery NTY0d9KM0Hw
watchlater recovery NTY0d9KM0Hw --raw
```

Where the federated response includes Filmot raw metadata, the tool normalizes its title, description, channel, upload date, duration and views into the effective catalogue view. If Filmot yields nothing and FindYouTubeVideo reports a PreserveTube copy, the tool makes one direct metadata-only request to PreserveTube's API and normalizes its title, description, channel and published date. Imported `[Private video]` / `[Deleted video]` snapshot rows remain unchanged; recovered data is stored separately with source provenance.

This means recovered videos can immediately participate in the cheap triage workflow:

```bash
watchlater creators --remaining
watchlater select title --contains "recovered phrase" --remaining
```

Further recovery work can add Wayback/watch-page parsing and other source-specific fallbacks for entries where neither Filmot nor PreserveTube can provide normalized metadata.

## Current CLI

Install the package and import one or more exports into a local SQLite catalogue. Each export is kept as a separate snapshot.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .

watchlater import watch-later.json
watchlater snapshots
watchlater creators --limit 30
```

By default the catalogue is stored in `watchlater.sqlite3`; use `--db PATH` before the subcommand to choose another location.

Creator grouping primarily uses stable YouTube channel IDs so renamed channels remain one cohort.

### Successive cohort triage

Cheap/manual classification is intended to happen before LLM work. Start with the largest creators, select an obvious cohort, record an action, then ask for the unresolved remainder again:

```bash
watchlater creators --remaining

# Prefer the stable channel ID shown by the creators report.
watchlater select creator UCo ... --remaining
watchlater selection show
watchlater selection action move --playlist "Queue - Electronics"

watchlater creators --remaining
```

You can also select by title substring or regular expression:

```bash
watchlater select title --contains "Super Mario" --remaining
watchlater selection action move --playlist "Queue - Games"

watchlater select title --regex 'conference|keynote' --remaining --max-duration 2h
watchlater selection action review
```

Selection filters include `--min-duration`, `--max-duration`, `--min-position` and `--max-position`. Durations accept seconds or suffixes such as `90s`, `15m`, `1.5h` and `2d`.

Actions are local catalogue decisions only at this stage; they do **not** mutate YouTube. Supported actions are `keep`, `review`, `archive`, `delete`, and `move`. A move records its intended destination playlist for later synchronisation.

Decisions are append-only history. Re-actioning a selected cohort supersedes its current decision without deleting the old event, and this makes it unresolved again while preserving history:

```bash
watchlater selection undo
```

`watchlater creators` shows current action counts and unresolved counts. `watchlater creators --remaining` excludes videos with a current decision, which supports successive refinement.

## Quick console method: clear the whole Watch Later playlist

> [!CAUTION]
> This is destructive. Export the playlist first. The script drives YouTube's web UI, so it may need updating if YouTube changes its page structure. It currently assumes an English-language YouTube UI.

Open <https://www.youtube.com/playlist?list=WL>, open the browser developer console, and paste:

```javascript
(async () => {
    if (!confirm("Remove every item from Watch Later? This cannot be undone in YouTube.")) {
        return;
    }

    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    let removed = 0;

    while (true) {
        const row = document.querySelector("ytd-playlist-video-renderer");
        if (!row) {
            console.log(`Finished. Removed ${removed} item(s).`);
            break;
        }

        const menuButton = row.querySelector(
            "ytd-menu-renderer button, #menu button, button[aria-label*='Action menu']"
        );

        if (!menuButton) {
            console.warn("Could not find the action menu for the next playlist item; stopping.", row);
            break;
        }

        menuButton.click();
        await sleep(400);

        const menuItems = [
            ...document.querySelectorAll(
                "ytd-menu-service-item-renderer, tp-yt-paper-item"
            )
        ];

        const removeItem = menuItems.find(item =>
            /remove from watch later/i.test(item.innerText || "")
        );

        if (!removeItem) {
            console.warn("Could not find 'Remove from Watch later'; stopping.");
            break;
        }

        removeItem.click();
        removed += 1;

        if (removed % 25 === 0) {
            console.log(`Removed ${removed} item(s)...`);
        }

        await sleep(750);
    }
})();
```

For selective cleanup, use the Python tooling instead of this all-or-nothing script.
