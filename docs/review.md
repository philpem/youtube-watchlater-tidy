# Human review report

`watchlater-review` provides a self-contained local HTML review workflow for inspecting and overriding catalogue decisions without depending on YouTube's playlist UI or running a web service.

The report is deliberately read-only with respect to SQLite. Human overrides are made in the browser, exported as a versioned JSON file, then explicitly imported with the CLI.

## Build a report

```bash
watchlater-review build review.html
```

For an older snapshot:

```bash
watchlater-review build review.html --snapshot 2
```

The resulting single HTML file embeds the catalogue rows and all filtering/sorting JavaScript. Thumbnail images remain ordinary YouTube/external image URLs rather than being copied into the report.

The report shows, where available:

- playlist position and video ID/link;
- thumbnail;
- immutable imported YouTube title;
- recovered archive title/provenance for unavailable videos;
- trusted DeArrow alternate title;
- channel/channel ID;
- duration, views, upload date and availability;
- current human/rule decision and reason;
- latest stored LLM suggestion, run ID, topic/content type/timeliness, confidence/reason, destination proposal and escalation flags.

Current catalogue decisions and LLM suggestions are displayed in separate columns. A stored LLM suggestion is never presented as though it were the current human decision.

## Filter and sort

The page can filter by free-text search, current action, LLM action, topic and maximum LLM confidence. Sorting includes playlist position, confidence and view count.

The maximum-confidence filter is useful for concentrating review on uncertain LLM results, e.g. confidence <= 0.6.

## Record overrides in the page

Each row has a human-override selector for:

- `keep`
- `review`
- `archive`
- `delete`

and an optional note field.

Leaving the selector at `no override` means the row is omitted from the exported decision file. The report therefore exports only choices the reviewer explicitly changed/confirmed during that review session.

`move` is intentionally not offered as a report override in this first version; playlist destinations need additional execution/synchronisation context from #10. Existing `move` decisions and LLM move proposals are still displayed.

## Export decisions

Press **Export explicit overrides** in the HTML page. The browser downloads JSON using this format marker:

```text
youtube-watchlater-tidy-review-decisions-v1
```

The payload includes the snapshot ID and a list of exact video IDs/actions/notes. It does not contain playlist-order assumptions.

## Validate before import

Always inspect with dry-run first:

```bash
watchlater-review import watchlater-review-2.json --dry-run
```

The importer validates:

- the format marker;
- integer snapshot ID;
- allowed action names;
- unique non-empty video IDs;
- that every video ID belongs to the specified snapshot.

Any missing/foreign video ID rejects the import before decisions are written.

## Apply the human overrides

```bash
watchlater-review import watchlater-review-2.json
```

Each change creates a normal append-only `decision_events` row with:

```text
source = human-review-report
```

and the report note as the decision reason. Existing decisions are superseded through normal catalogue history rather than deleted.

Re-importing an identical file is idempotent while the same imported human-review decision is still current: an identical action/reason is reported as unchanged and no duplicate event is appended.

## What this does not do

The review report does not mutate YouTube. In particular, choosing `delete` or `archive` only creates a local reviewed decision. Selective Watch Later execution remains #7, and destination-playlist execution remains #10.
