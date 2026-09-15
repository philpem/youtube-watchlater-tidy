# DeArrow enrichment

`youtube-watchlater-tidy` can optionally fetch alternate title submissions from the
[DeArrow](https://dearrow.ajay.app/) project using its read-only branding API.
DeArrow is developed by Ajay Ramachandran and contributors and is closely related
to the SponsorBlock ecosystem.

The enrichment code does **not** submit titles, vote on submissions, or modify
DeArrow data. It only performs `GET` requests and stores the returned evidence in
the local catalogue.

## API endpoints

The direct lookup mode uses:

```text
GET https://sponsor.ajay.app/api/branding?videoID=<VIDEO_ID>&fetchAll=true
```

The optional privacy-preserving mode hashes the YouTube video ID locally and
queries the four-hex-character SHA-256 prefix endpoint:

```text
GET https://sponsor.ajay.app/api/branding/<HASH_PREFIX>?fetchAll=true
```

The response can contain several videos sharing the prefix; only the exact video
ID requested by the local catalogue is retained.

## Title provenance

The imported YouTube title is never overwritten. Each DeArrow lookup stores:

- the complete raw API response;
- the normalized title submission list;
- title text, votes, `locked`, `original`, and submission UUID fields;
- the lookup timestamp and API URL/mode;
- a separately derived preferred alternate title, when one is trusted.

DeArrow returns title submissions in preferred/quality order. This tool accepts
the first submission as a preferred **alternate** only when it is locked or has
non-negative votes. A first submission marked `original=true` deliberately does
not become an alternate title: it means the original YouTube title remains the
appropriate title.

## Caching and privacy

Found and not-found responses are cached locally. `--max-age` gives the cache a
TTL, while `--refresh` explicitly refreshes the selected target. Errors are not
considered durable negative results.

The hash-prefix mode avoids sending the exact video ID to the DeArrow API, at the
cost of receiving the small bucket of records sharing that prefix.

## Licensing and attribution

The DeArrow software repository is distributed under the GNU GPL v3; see
<https://github.com/ajayyy/DeArrow> and its `LICENSE` file.

The title submissions returned by the service are user-contributed data. This
project caches that data for local analysis and does not assert that the GPLv3
software licence automatically governs redistribution of the returned dataset.
Check DeArrow/SponsorBlock's current terms and data-licensing guidance before
redistributing a bulk cache or derived dataset.
