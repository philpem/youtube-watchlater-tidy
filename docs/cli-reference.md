# Command-line map

The operator guide presents the recommended sequence. This page maps the installed command
families so less common operations are easier to discover.

Every command supports `--help`, and each subcommand has its own help:

```bash
watchlater --help
watchlater recover --help
watchlater-playlist execute --help
watchlater-remove execute --help
```

The installed version's `--help` output is authoritative for individual options.

## Progress and long-running commands

Long-running network, browser and LLM operations report progress on **stderr** so
machine-readable stdout remains clean. Commands that support the common progress policy
accept:

```text
--progress auto|always|never
```

`auto` uses a live progress display on an interactive terminal and concise milestone
updates when stderr is redirected. `always` forces progress rendering where possible,
while `never` suppresses it. Existing `--no-progress` options remain accepted as a
compatibility alias for `--progress never`.

LLM annotation with `--taxonomy discover` reports taxonomy discovery separately from
the subsequent annotation batches. Provider retry/backoff periods are also surfaced so a
deliberate retry delay does not look like a hung process.

## Catalogue and deterministic triage

```text
watchlater import       import an immutable yt-dlp snapshot
watchlater snapshots    list imported snapshots
watchlater creators     aggregate by stable creator/channel identity
watchlater keywords     report title words or n-grams
watchlater videos       list individual/effective catalogue rows
watchlater enrich       repair selected live-video metadata with yt-dlp
watchlater recover      query archives for deleted/private entries
watchlater recovery     inspect one cached archive result
watchlater select       create creator/title cohort selections
watchlater selection    show, action, undo, export or save a selection as a rule
watchlater rules        list, enable, disable or apply saved rules
```

Place the catalogue option before the subcommand:

```bash
watchlater --db /path/to/catalogue.sqlite3 creators --remaining
```

The default is `watchlater.sqlite3` in the current directory.

## Optional evidence and classification

```text
watchlater-dearrow         fetch and inspect trusted alternate-title evidence
watchlater-metadata        fetch richer yt-dlp metadata/descriptions
watchlater-transcript      fetch and inspect existing caption evidence
watchlater-llm             inspect providers; classify actions; annotate categories/tags; save/reuse taxonomies; refine descriptions
watchlater-llm-transcript  perform transcript-aware classification refinement
```

LLM commands require a project TOML configuration. `watchlater-llm annotate` stores
semantic category/tag evidence separately from action classifications and supports
`--scope all`, `--scope remaining`, configured vocabularies, taxonomy discovery and
reuse of saved discovered taxonomies. `watchlater-llm taxonomies` lists saved
vocabularies, `watchlater-llm taxonomy --taxonomy-id ID` inspects one, and
`watchlater-llm annotation-results` inspects a stored annotation run. See the
[provider guide](llm.md) and [classification guide](llm-classification.md).

## Human review

```text
watchlater-review build    generate a self-contained paginated HTML report
watchlater-review import   validate/import explicit overrides exported by the report
```

HTML review changes the local catalogue only after its exported JSON is imported.

## Destination playlists

This command family is only for `move` decisions. An `archive`/`delete`-only cleanup starts
with `watchlater-remove plan` and does not require destination inventory.

```text
watchlater-playlist inventory import    import a versioned inventory JSON file
watchlater-playlist inventory show      show the current local inventory
watchlater-playlist inventory refresh   fetch inventory using YouTube Data API OAuth
watchlater-playlist browser-login       verify an attached browser session
watchlater-playlist assign              assign one selection to one or more destinations
watchlater-playlist plan                persist an API or browser execution plan
watchlater-playlist show                inspect a persisted plan
watchlater-playlist execute             dry-run or explicitly apply a persisted plan
```

`plan` prints a top-level `run_id`. That integer—not the optional output filename—is the
plan ID supplied to `show --run-id` and `execute --run-id`. Omitting `--run-id` selects the
latest suitable plan, but explicit IDs are clearer and safer.

## Watch Later removal

Use this command family directly for removal-only workflows. `archive` and `delete`
decisions do not need a preceding playlist plan or destination inventory; `move` decisions
remain blocked until their destination checkpoints are confirmed.

```text
watchlater-remove plan      persist a removal plan from current local decisions
watchlater-remove show      inspect a persisted removal plan
watchlater-remove login     verify an attached browser session
watchlater-remove execute   dry-run or explicitly apply a persisted removal plan
```

Removal plan IDs work like playlist plan IDs: use the top-level `run_id` printed by
`watchlater-remove plan`. A destructive execution requires both `--apply` and
`--confirm-remove`. `execute` prints a concise summary by default; add `--json` for
the full machine-readable execution result and plan.

## Identity placeholders used in the guides

Examples use these placeholders:

| Placeholder | Meaning |
| --- | --- |
| `SNAPSHOT_ID` | Integer printed by `watchlater import`/`watchlater snapshots` |
| `SELECTION_ID` | Integer printed by a `watchlater select ...` command |
| `PLAN_ID`, `API_PLAN_ID`, `BROWSER_PLAN_ID` | Top-level `run_id` printed by `watchlater-playlist plan` |
| `REMOVAL_PLAN_ID` | Top-level `run_id` printed by `watchlater-remove plan` |
| `VIDEO_ID` | Exact 11-character YouTube video ID |
| `CHANNEL_ID` | Stable YouTube channel ID shown by creator reports |
