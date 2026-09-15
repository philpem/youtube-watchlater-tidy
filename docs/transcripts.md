# Caption and transcript evidence

Transcript acquisition is an optional late-stage escalation for the small subset of unresolved
videos whose LLM result says `needs_transcript=true` after cheaper evidence has already been
tried.

The implementation uses **existing subtitle/caption tracks only**. It does not download
video/audio media and does not run speech-to-text over audio.

## Source preference

yt-dlp exposes creator/provider subtitle tracks separately from `automatic_captions`. The
selection policy is:

1. choose a matching normal/manual subtitle track when available;
2. otherwise use a matching automatically generated caption track when automatic fallback is
   enabled;
3. otherwise store a `not_found` transcript observation.

A missing transcript is **not** a negative quality signal. It only means the requested caption
policy did not yield usable text.

## Fetch caption evidence from an LLM run

For a stored run whose output contains `needs_transcript=true`:

```bash
watchlater-transcript fetch --llm-needs-transcript --run-id 18 --dry-run
watchlater-transcript fetch --llm-needs-transcript --run-id 18
```

For the latest stored run, omit `--run-id`.

Other explicit targets are also available:

```bash
watchlater-transcript fetch --video-id VIDEO_ID
watchlater-transcript fetch --video-id ID1 --video-id ID2
watchlater-transcript fetch --selection 12
watchlater-transcript fetch --all --limit 10
```

By default decided videos are skipped. Use `--include-decided` only when you specifically want
caption evidence despite an existing human/rule decision.

## Languages and automatic captions

English is the default preference. Repeat `--language` for ordered alternatives:

```bash
watchlater-transcript fetch --video-id VIDEO_ID \
    --language en-GB --language en --language fr
```

Automatic captions are a fallback by default. Disable them with:

```bash
watchlater-transcript fetch --video-id VIDEO_ID --no-auto
```

The language list plus the automatic-caption policy form part of the transcript lookup cache
key, so changing either policy is a logically different lookup.

## Formats, storage and provenance

For the chosen caption track the implementation prefers `json3`, then WebVTT. It fetches the
caption data/URL exposed by yt-dlp rather than asking yt-dlp to write media or subtitle files to
the working directory.

The cache retains:

- source type (`manual` or `automatic`);
- language tag and display name;
- selected format;
- normalized transcript text;
- normalized segment records (with timestamps where available);
- the raw caption payload;
- caption-source URL where applicable;
- a summary of available manual/automatic languages;
- fetch time and request-policy hash.

The transcript cache table is created automatically on first transcript use. Successful and
`not_found` observations are cached for the same language/automatic policy. Errors remain
retriable.

Inspect a successful cached transcript:

```bash
watchlater-transcript show VIDEO_ID
watchlater-transcript show VIDEO_ID --raw
```

Force a new lookup for the chosen target with `--refresh`.

## Concurrency and pacing

The default uses four workers while spacing yt-dlp metadata request starts by 0.5 seconds:

```bash
watchlater-transcript fetch --llm-needs-transcript --run-id 18 \
    --workers 4 --interval 0.5
```

Reduce concurrency or increase the interval if YouTube starts throttling.

## Transcript-aware LLM refinement

After captions are cached, refine the exact parent run with:

```bash
watchlater-llm-transcript \
    --config watchlater.toml \
    --run-id 18 \
    --provider ollama-local \
    --dry-run

watchlater-llm-transcript \
    --config watchlater.toml \
    --run-id 18 \
    --provider ollama-local
```

Only videos in parent run 18 that both have `needs_transcript=true` and remain unresolved are
eligible. Videos without a successful cached transcript are reported and skipped; the earlier
classification remains intact.

Each refinement input contains:

- the parent run ID;
- previous evidence;
- previous validated classification;
- transcript text;
- manual/automatic source type;
- language and caption format;
- transcript fetch time and request-policy hash;
- whether the transcript was truncated for model context.

### Long transcripts

The default model context budget is 12,000 transcript characters per video. Longer transcripts
are deterministically sampled from the **beginning, middle and end**, rather than simply
truncated at the front. The original cached transcript remains untouched.

Change the model-side budget with:

```bash
watchlater-llm-transcript \
    --config watchlater.toml \
    --run-id 18 \
    --max-transcript-chars 24000
```

Changing the character budget changes the evidence hash and therefore the exact-run cache key.
If a sampled transcript remains insufficient, the model may keep `needs_transcript=true`; a
larger-budget rerun then becomes a distinct child run.

### History, cache and precedence

Transcript refinement uses the same validated OpenAI-compatible provider layer and append-only
schema-v7 LLM store as the other classifier stages. The child run context records:

```text
stage = transcript_refinement
parent_run_id = <parent>
max_transcript_chars = <budget>
```

The original parent run is never changed. A human or saved-rule decision still outranks the
stored LLM child suggestion. `--refresh` appends another child run; `--no-store` performs an
ephemeral provider call.

This completes the description/transcript escalation ladder without automatically converting
LLM output into catalogue decisions or YouTube actions.
