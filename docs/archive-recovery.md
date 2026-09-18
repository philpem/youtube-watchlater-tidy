# Recover deleted and private video metadata

An unavailable Watch Later row normally retains its 11-character YouTube video ID even
when YouTube exposes only `[Deleted video]` or `[Private video]`. Archive recovery uses that
exact ID to look for historical metadata and archived copies without rewriting the imported
snapshot.

## Recovery pipeline

The current pipeline queries FindYouTubeVideo v5, which federates several public archive
and index services. If it finds useful evidence, metadata is normalized in increasing-cost
order:

1. Filmot metadata already embedded in the FindYouTubeVideo response;
2. PreserveTube metadata, only when the federated response reports a PreserveTube copy;
3. the specific Wayback watch page discovered by FindYouTubeVideo.

Wayback parsing understands `ytInitialPlayerResponse`, older `ytplayer.config` data and
legacy page metadata. The effective catalogue may recover title, description, creator,
upload date, duration and view count where a source provides them.

The original snapshot row remains `[Deleted video]` or `[Private video]`. Every recovered
observation retains its source and raw response so conflicting or incomplete evidence is
auditable.

## Preview targets

Start without network requests:

```bash
watchlater recover --unavailable --dry-run
```

Limit an incremental run:

```bash
watchlater recover --unavailable --limit 10
```

Cached found and not-found results are excluded before `--limit`, so repeating this command
advances through the uncached remainder rather than revisiting the first ten entries.

Other target forms are:

```bash
# one or several exact IDs
watchlater recover --video-id VIDEO_ID
watchlater recover --video-id VIDEO_ID_1 --video-id VIDEO_ID_2

# unavailable entries already grouped in a saved selection
watchlater recover --selection 12

# a playlist-position range
watchlater recover --unavailable --min-position 1000 --max-position 1500
```

Use `--snapshot ID` when the latest snapshot is not the intended target.

## Inspect results

Show recovery state across unavailable entries:

```bash
watchlater videos --unavailable
watchlater videos --unavailable --recovered
watchlater videos --unavailable --unrecovered
```

Inspect one cached result:

```bash
watchlater recovery VIDEO_ID
watchlater recovery VIDEO_ID --raw
```

`--raw` prints the complete cached federated response for investigation. It does not make
another network request.

Recovered metadata participates in later creator/title reports, selections and LLM evidence.
When a backend explicitly reports an archived video resource, the HTML review report labels
it **Recovered video**. Metadata-only archive links are not presented as playable copies.

## Cache and refresh behavior

Successful and not-found federated lookups are durable cache hits. Use `--refresh` only for
an explicitly selected target when you want to repeat the network lookup:

```bash
watchlater recover --video-id VIDEO_ID --refresh
```

A failed request is not proof that no archive exists. Network/service failures remain
retriable. FindYouTubeVideo can take tens of seconds because it queries several backends;
streamed service progress is shown unless `--no-progress` is supplied.

Use `--timeout SECONDS` for slow services and `--base-url URL` when using another compatible
FindYouTubeVideo deployment. If the public service returns HTTP 429, stop and retry later
with a smaller explicit batch rather than repeatedly refreshing cached targets.

## Privacy and provenance

Archive queries disclose the selected video IDs to the configured archive service. The
tool does not upload the Watch Later playlist or its local decisions as a whole.

Archive/index data can be wrong or historical. Treat recovered metadata as evidence, inspect
its provenance, and make the final keep/archive/delete decision yourself.

