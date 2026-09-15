# Selective richer YouTube metadata

The initial Watch Later import intentionally uses `yt-dlp --flat-playlist` so a ~5000-item
catalogue can be created quickly. Full per-video extraction is much slower, so richer
metadata is fetched only when it is useful.

`watchlater-metadata` runs yt-dlp with metadata-only options; it does not download video or
audio media. Successful results are stored in the existing `metadata_observations` table
with `source='yt-dlp'`, preserving the full raw yt-dlp JSON as well as normalized fields
such as description, upload date, availability, title/channel, duration and view count.
Fields such as `live_status` remain available in the raw JSON even where the catalogue does
not normalize them into a dedicated column.

## Targets

Fetch any uncached live entries in a snapshot:

```bash
watchlater-metadata enrich --all
```

Target an explicit subset or saved cohort:

```bash
watchlater-metadata enrich --video-id ABC123 --video-id DEF456
watchlater-metadata enrich --selection 12
```

Fetch only entries that still lack a description:

```bash
watchlater-metadata enrich --missing-description
```

The LLM classifier can request escalation with `needs_description=true`. Feed exactly that
subset from a stored classification run into yt-dlp:

```bash
watchlater-metadata enrich --llm-needs-description
watchlater-metadata enrich --llm-needs-description --run-id 12
```

If the description has already been populated since the classification run, that item is
skipped unless `--refresh` is requested.

## Caching and failures

A successful yt-dlp observation is considered cached. Normal repeated runs skip it;
`--refresh` deliberately refetches the selected target. Error observations are retained for
audit/debugging but do not become durable cache hits, so transient failures can be retried.

Private/deleted source markers are skipped from broad/selection-based enrichment because
current YouTube extraction cannot recover them; archive recovery handles those separately.
An explicitly requested video ID is still allowed and will record its yt-dlp failure if it
is unavailable.

Each video fails independently. One yt-dlp error does not abort a batch.

## Concurrency and pacing

Full YouTube extraction is much heavier than the flat playlist import. The default is four
workers with at least 0.5 seconds between request starts:

```bash
watchlater-metadata enrich --missing-description --workers 4 --interval 0.5
```

Use a larger interval or fewer workers if YouTube starts throttling the client. `--limit`
and `--dry-run` are useful before a large enrichment pass.

Inspect the latest cached yt-dlp result for one video:

```bash
watchlater-metadata show VIDEO_ID
watchlater-metadata show VIDEO_ID --raw
```
