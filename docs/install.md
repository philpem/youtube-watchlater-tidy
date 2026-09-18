# Installation, updates and local data

The project currently runs directly from a source checkout. There is no published
PyPI package or tagged release yet.

## Core installation

Python 3.10 or newer is required:

```bash
git clone https://github.com/philpem/youtube-watchlater-tidy.git
cd youtube-watchlater-tidy

python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The editable install exposes the `watchlater` command and the related specialist
commands documented in the [CLI reference](cli-reference.md).

## Optional features

Install only the integrations you intend to use:

```bash
# YouTube Data API playlist inventory and writes
python -m pip install -e '.[youtube-api]'

# Playwright-based playlist and Watch Later operations
python -m pip install -e '.[browser]'

# Local MkDocs documentation preview
python -m pip install -e '.[docs]'
```

For legacy Playwright-launched browser mode, also install its Chromium build:

```bash
playwright install chromium
```

The recommended CDP workflow instead attaches to a Chrome/Chromium process that
you launch and authenticate manually. See the [browser backend guide](playlist-browser.md).

## Local files and backups

The default catalogue is `watchlater.sqlite3`. It contains viewing-history metadata,
decisions, cached enrichment and execution checkpoints. The original yt-dlp export
and the catalogue should both be treated as private data.

Before an upgrade or any large external run, make copies of:

- `watch-later.json` (and later snapshots);
- `watchlater.sqlite3`;
- any explicit plan/report JSON files you want to retain.

OAuth credentials, OAuth token caches and browser profiles contain secrets. They are
ignored by the repository and must not be committed or shared:

- `client_secret*.json`;
- `.youtube-watchlater-token.json`;
- `.watchlater-playwright-profile/`;
- any dedicated Chrome/Chromium CDP profile directory.

## Updating a source checkout

Stop any running import, enrichment or executor process before updating. Back up the
catalogue, then update and reinstall the editable package:

```bash
git pull --ff-only
. .venv/bin/activate
python -m pip install -e .
```

Add the appropriate optional extra to the last command if you use it. Catalogue schema
migrations run automatically when the database is opened. The program refuses a database
whose schema is newer than the installed code rather than attempting to downgrade it.

After updating, use `COMMAND --help` to check any command whose workflow you are about
to run. For external writes, create a fresh plan if an old plan is reported as stale.

