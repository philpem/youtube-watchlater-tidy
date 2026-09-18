# YouTube Data API playlist workflow

The YouTube Data API backend inventories normal playlists and applies reviewed `move`
decisions without driving YouTube's web interface. It is appropriate for modest batches
where API quota is available. For large batches, use the
[browser playlist backend](playlist-browser.md) instead.

This backend never removes items from Watch Later. Destination synchronization must
complete first; source removal remains a separate, explicitly confirmed operation.

## 1. Install API support

```bash
python -m pip install -e '.[youtube-api]'
```

## 2. Create Google OAuth credentials

The tool uses Google's installed-application OAuth flow. Follow Google's
[YouTube installed-app authorization guide](https://developers.google.com/youtube/v3/guides/auth/installed-apps)
and, in Google Cloud Console:

1. create or select a project;
2. enable **YouTube Data API v3** for that project;
3. configure the OAuth consent screen;
4. create an OAuth client ID with application type **Desktop app**;
5. download the client JSON.

Save the downloaded file as `client_secret.json` in the directory from which you run the
tool, or pass its path explicitly with `--client-secrets PATH`.

The repository ignores `client_secret*.json`, but still treat this file as a secret and
never commit or share it.

The application requests this scope:

```text
https://www.googleapis.com/auth/youtube.force-ssl
```

That scope permits playlist reads and writes. The tool does not request account passwords;
Google performs consent in the user's browser.

## 3. Authorize and refresh playlist inventory

The first authenticated command opens the Google consent flow:

```bash
watchlater-playlist inventory refresh
```

After authorization, the credentials are cached by default in:

```text
.youtube-watchlater-token.json
```

The token file is written with user-only permissions where the operating system supports
them. Later commands reuse and refresh it. Custom locations are supported:

```bash
watchlater-playlist inventory refresh \
    --client-secrets /secure/path/client.json \
    --token /secure/path/watchlater-token.json
```

Inspect the imported inventory:

```bash
watchlater-playlist inventory show
```

Inventory refresh reads every owned playlist and its membership, so it may take some time
on an account with many playlists. It does not change YouTube.

## 4. Record reviewed move decisions

For one destination:

```bash
watchlater selection action move --playlist 'Queue - Electronics'
```

For several destinations attached to the same selected cohort:

```bash
watchlater-playlist assign \
    --selection 12 \
    --playlist 'Queue - Electronics' \
    --playlist 'Reference - Repairs'
```

Only current local `move` decisions enter a playlist plan. LLM suggestions alone never
authorize API writes.

## 5. Create and identify an API plan

```bash
watchlater-playlist plan --backend api --output api-plan.json
```

The command persists the plan in `watchlater.sqlite3` and prints its JSON. The top-level
`run_id` is the value used by later commands. For example, if the output contains:

```json
{
  "backend": "api",
  "run_id": 7,
  "status": "planned"
}
```

then `API_PLAN_ID` in examples means `7`:

```bash
watchlater-playlist show --run-id 7
```

The optional `--output` file is a human-inspectable copy; it is not the plan database.
If `--run-id` is omitted, `show` and `execute` use the latest plan for the selected/latest
snapshot, but explicit IDs are safer when several plans exist.

The planner reports expected playlist creations, insertions and estimated quota. It refuses
an over-limit API plan unless `--allow-over-quota` is supplied.

## 6. Dry-run execution

Inspect what the persisted plan would do without creating an OAuth client or writing to
YouTube:

```bash
watchlater-playlist execute --run-id 7
```

If decisions changed after planning, affected items are stale. Create a new plan rather
than applying an obsolete one.

## 7. Apply in small batches

Start with a conservative write cap:

```bash
watchlater-playlist execute --run-id 7 --apply --max-writes 3
```

The executor:

- resolves or creates exact destination playlists;
- rechecks the exact current decision before each write;
- checks live membership before insertion;
- records `inserted`, live-verified `already_present`, failures and timestamps;
- resumes without repeating confirmed work.

Repeat the same command to resume a partial plan. `--max-writes` counts playlist creations
and video insertions; read-only checks do not consume that cap.

To use non-default credential paths during execution, pass the same `--client-secrets` and
`--token` options used for inventory refresh.

## 8. Remove confirmed items from Watch Later

Successful destination synchronization does not remove the source item. Once every
destination on an exact `move` decision is confirmed, build and inspect a removal plan:

```bash
watchlater-remove plan
watchlater-remove show
```

Watch Later removal uses the browser executor and requires its own dry-run and explicit
destructive confirmation. Continue with the [Watch Later removal guide](watchlater-removal.md).

## Quota and credential recovery

YouTube assigns quota costs to API operations. The planner defaults are configurable and
are estimates; Google Cloud Console is authoritative for the account's current allowance.
See Google's [YouTube Data API overview](https://developers.google.com/youtube/v3/getting-started)
for project and quota background.

If consent was granted to the wrong account, the token is corrupt, or access was revoked,
move the token file aside and run `inventory refresh` again to perform a fresh consent flow.
Do not delete `client_secret.json`; it identifies the desktop OAuth client, while the token
cache represents the user's authorization.

See [Troubleshooting](troubleshooting.md) for common OAuth, quota and plan errors.
