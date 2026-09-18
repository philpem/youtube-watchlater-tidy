# Troubleshooting

Start by confirming that the intended virtual environment is active and inspect the exact
command's help:

```bash
. .venv/bin/activate
COMMAND --help
```

Use `--db PATH` consistently when the catalogue is not `watchlater.sqlite3` in the current
directory. Many apparent “missing plan/selection” errors come from opening a different
catalogue.

## Google blocks browser login

Do not sign in through a Playwright-launched browser. Launch an ordinary dedicated
Chrome/Chromium profile yourself:

```bash
google-chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.local/share/watchlater-chrome"
```

Sign in manually, leave that browser running, and attach with:

```bash
watchlater-playlist browser-login --cdp-endpoint http://127.0.0.1:9222
```

Keep the remote-debugging profile separate from your normal browser profile.

## The CDP endpoint cannot be reached

Check that the browser was started with both `--remote-debugging-port` and a dedicated
`--user-data-dir`, and that the port matches `--cdp-endpoint`. Chrome/Chromium may refuse
remote debugging against its normal default profile.

The executor opens its own tab in the attached browser. Closing the browser or disabling
the DevTools endpoint ends the session.

## OAuth client secrets file does not exist

The API backend defaults to `client_secret.json` in the current directory. Download a
**Desktop app** OAuth client from Google Cloud Console, or pass its path:

```bash
watchlater-playlist inventory refresh --client-secrets /secure/path/client.json
```

See the [YouTube Data API guide](youtube-api.md) for complete setup.

## OAuth consent, invalid token or wrong account

The default authorization cache is `.youtube-watchlater-token.json`. If it belongs to the
wrong account, is corrupt, or its authorization was revoked, move it aside and repeat
`inventory refresh` to perform a new consent flow. Preserve the OAuth client JSON.

If Google reports that the application is in testing, ensure the intended Google account
is an allowed test user on the OAuth consent screen.

## API quota or permission errors

Inspect the persisted plan's quota estimate before applying it:

```bash
watchlater-playlist show --run-id PLAN_ID
```

An estimate exceeding the configured limit is refused unless `--allow-over-quota` is
explicitly supplied. A Google HTTP 403 can also mean the YouTube Data API is disabled,
the authenticated account lacks access, or the cloud project's daily quota is exhausted.

For very large move batches, consider the browser backend, which uses no YouTube Data API
write quota.

## Where a plan ID comes from

Both plan commands print JSON containing a top-level `run_id` and persist the same plan in
SQLite:

```bash
watchlater-playlist plan --backend browser
watchlater-remove plan
```

If playlist planning prints `"run_id": 7`, use `--run-id 7`. The optional `--output` file
is only a copy for inspection. `show` without an ID displays the latest applicable plan.

## A plan is stale

Plans bind every item to the exact current decision event. Changing an action or destination
after planning intentionally makes the old item stale. Do not force it; create and review a
new plan.

## Playlist title is ambiguous

YouTube permits several playlists with the same title. The planner refuses to guess which
one is intended. Rename the playlists so the destination title is unique, refresh/import
inventory, and create a new plan.

## Browser executor cannot find a row, menu or label

YouTube UI structure and localized labels can change. Keep execution headed and start with
a very small write/delete cap. The commands expose label and scrolling overrides; inspect:

```bash
watchlater-playlist execute --help
watchlater-remove execute --help
```

The executor fails closed when it cannot verify exact video/destination identity. Do not
weaken those identity checks to work around UI drift.

## Archive recovery is slow or rate-limited

FindYouTubeVideo queries several services and one video can take tens of seconds. Streamed
backend progress is expected. Use a limited batch and allow it to complete:

```bash
watchlater recover --unavailable --limit 10
```

HTTP 429 means the archive service has rate-limited the request. Stop and retry later;
avoid `--refresh` unless the exact cached target genuinely needs another lookup.

## yt-dlp enrichment fails for one video

Metadata enrichment records failures per video and continues the batch. Confirm yt-dlp is
current, retry one exact ID, and inspect the live video's availability:

```bash
watchlater enrich --video-id VIDEO_ID --refresh
```

Private and deleted entries belong to archive recovery rather than live yt-dlp enrichment.

## HTML review changes disappeared

Selections in the report live only in browser memory until **Export explicit overrides** is
clicked. Dry-run and then import the downloaded JSON:

```bash
watchlater-review import watchlater-review-SNAPSHOT_ID.json --dry-run
watchlater-review import watchlater-review-SNAPSHOT_ID.json
```

Regenerate the HTML report to verify the imported current decisions.

