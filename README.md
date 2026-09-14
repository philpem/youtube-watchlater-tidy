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

## Current CLI

The first implementation imports one or more exports into a local SQLite catalogue while preserving each export as a separate snapshot.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .

watchlater import watch-later.json
watchlater snapshots
watchlater creators --limit 30
```

By default the catalogue is stored in `watchlater.sqlite3`; use `--db PATH` before the subcommand to choose another location.

The `creators` report is intentionally read-only at this stage. It groups primarily by stable YouTube channel ID so that renamed channels remain one cohort. Later work will add cohort decisions, playlist moves, DeArrow enrichment and LLM classification.

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

        // Be deliberately conservative so the page has time to update and so
        // we do not hammer YouTube's UI as quickly as JavaScript can run.
        await sleep(750);
    }
})();
```

For selective cleanup, use the Python tooling planned in the repository issues instead of this all-or-nothing script.
