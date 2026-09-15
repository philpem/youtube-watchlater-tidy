# Caption and transcript evidence

Transcript acquisition is an optional late-stage escalation. It is intended for the small
subset of unresolved videos where an LLM result says `needs_transcript=true` after cheaper
evidence has already been tried.

The default implementation uses **existing subtitle/caption tracks only**. It does not
download video/audio media and does not run speech-to-text over audio.

## Source preference

yt-dlp exposes creator/provider subtitle tracks separately from `automatic_captions`.
`youtube-watchlater-tidy` therefore uses this policy:

1. choose a matching normal/manual subtitle track when available;
2. otherwise, use a matching automatically generated caption track when automatic fallback
   is enabled;
3. otherwise store a `not_found` transcript observation.

A missing transcript is **not** a negative quality signal. It only means the requested
caption policy did not yield usable text.

## Fetch from an LLM run

For the latest stored run whose output contains `needs_transcript=true`:

```bash
watchlater-transcript fetch --llm-needs-transcript --dry-run
watchlater-transcript fetch --llm-needs-transcript
```

For a specific run:

```bash
watchlater-transcript fetch --llm-needs-transcript --run-id 18
```

By default only unresolved videos are included. Use `--include-decided` only when you
specifically want transcript evidence even though a human/rule decision already exists.

## Other targets

```bash
watchlater-transcript fetch --video-id VIDEO_ID
watchlater-transcript fetch --video-id ID1 --video-id ID2
watchlater-transcript fetch --selection 12
watchlater-transcript fetch --all --limit 10
```

The last form is intentionally explicit: transcript acquisition is normally expected to be
selective rather than a whole-catalogue preprocessing step.

## Languages and automatic captions

English is the default preference. Repeat `--language` for ordered alternatives:

```bash
watchlater-transcript fetch --video-id VIDEO_ID \
    --language en-GB --language en --language fr
```

Language tags that share the requested base language are accepted, so a manual track with
a YouTube-generated tag such as `en-...` can still satisfy an `en` request.

Automatic captions are a fallback by default. Disable them with:

```bash
watchlater-transcript fetch --video-id VIDEO_ID --no-auto
```

The language list plus the automatic-caption policy form part of the transcript lookup
cache key. Changing either policy performs a logically different lookup.

## Formats and parsing

For the chosen caption track the implementation prefers `json3`, then WebVTT. It fetches
the caption data/URL exposed by yt-dlp rather than asking yt-dlp to write files to the
working directory.

The cache retains:

- source type (`manual` or `automatic`);
- language tag and display name;
- selected format;
- normalized transcript text;
- normalized segment records (with timestamps where the source format exposes them);
- the raw caption payload;
- caption-source URL where applicable;
- a summary of available manual/automatic languages;
- fetch time and request-policy hash.

The transcript cache table is created automatically on first transcript use.

## Concurrency and pacing

The default uses four workers while spacing yt-dlp metadata request starts by 0.5 seconds:

```bash
watchlater-transcript fetch --llm-needs-transcript \
    --workers 4 --interval 0.5
```

Reduce concurrency/increase the interval if YouTube starts throttling.

## Cache and refresh

Successful and `not_found` observations are cached for the same language/automatic policy.
Errors are not treated as durable negative results.

Force a new lookup for the chosen target with:

```bash
watchlater-transcript fetch --video-id VIDEO_ID --refresh
```

Inspect a successful cached transcript:

```bash
watchlater-transcript show VIDEO_ID
watchlater-transcript show VIDEO_ID --raw
```

## Next stage

Transcript acquisition only stores evidence. The next classifier stage consumes a stored
transcript for a parent LLM run's `needs_transcript=true` videos, caps/chunks the text for
context safety, and stores another append-only refinement run. No transcript result is ever
promoted directly into a human/rule decision.