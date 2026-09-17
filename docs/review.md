# Human review report

`watchlater-review` provides a self-contained HTML workflow for making **local human decisions** without changing YouTube directly.

The complete workflow is:

```text
build HTML
   ↓
review rows in a browser
   ↓
choose explicit overrides
   ↓
export JSON
   ↓
dry-run import
   ↓
import into SQLite
   ↓
later plan/apply YouTube changes separately
```

## Build a report

```bash
watchlater-review build review.html
firefox review.html
```

For another snapshot:

```bash
watchlater-review build review.html --snapshot 2
```

The HTML is self-contained apart from ordinary external thumbnail URLs. It embeds the catalogue data and filtering/sorting JavaScript.

It displays, where available:

- playlist position and exact video ID/link;
- immutable imported title;
- recovered metadata for unavailable videos;
- direct archive links labelled **Recovered video** when the recovery backend reports an actual archived video resource;
- trusted DeArrow alternate title;
- channel identity;
- duration, views, upload date and availability;
- the current human/rule decision;
- the latest stored LLM suggestion and its provenance.

Current decisions and LLM suggestions are deliberately separate. An LLM suggestion is never shown as though it were already the current decision.

Recovered-video links are taken only from archive resources whose recovery metadata explicitly says they contain video. Metadata-only sources are not presented as playable recovered video links. If several archives contain the video, the report shows all matching services.

## Filter and sort

The page supports free-text search, current-action filtering, LLM-action filtering, topic filtering, maximum LLM confidence, and position/confidence/view sorting.

The confidence filter is useful for focusing on uncertain LLM results.

## Human overrides

The rightmost **Human override** column currently offers:

- `keep`
- `review`
- `archive`
- `delete`

plus an optional note.

### What the choices mean

- **no override** — do not change the current catalogue decision;
- **keep** — explicitly keep the video in Watch Later;
- **review** — explicitly defer it for further manual review;
- **archive** — retain it as useful local/reference material, but later remove it from Watch Later;
- **delete** — no longer wanted; later remove it from Watch Later.

The page keeps choices only in browser memory until they are exported. Only rows with an explicit override are included in the exported JSON.

## Export the overrides

Click **Export explicit overrides**. The browser downloads a file such as:

```text
watchlater-review-1.json
```

using format:

```text
youtube-watchlater-tidy-review-decisions-v1
```

The file contains exact video IDs, actions and optional notes. It does not use playlist positions as the identity of a video.

## Dry-run the import

Always validate first:

```bash
watchlater-review import watchlater-review-1.json --dry-run
```

This checks the format marker, snapshot ID, allowed actions, duplicate video IDs, and that every video belongs to the selected snapshot. No decision rows are written during the dry-run.

## Import into the local catalogue

```bash
watchlater-review import watchlater-review-1.json
```

Each changed row creates an append-only decision event with:

```text
source = human-review-report
```

The optional note becomes the reason. Existing decisions are superseded through normal decision history rather than erased. Re-importing the same file is idempotent while the identical imported decision is still current.

## Verify the import

Regenerate the report:

```bash
watchlater-review build review-after.html
firefox review-after.html
```

The imported choices should now appear in the **Current decision** column with `human-review-report` as their source.

## What happens next

Importing review overrides still does **not** change YouTube.

The external action depends on the decision:

- `keep` / `review` — no external operation;
- `archive` / `delete` — become candidates for `watchlater-remove plan`;
- `move` — is currently recorded outside the HTML report because destinations are required.

For moves, use normal CLI selection/assignment:

```bash
watchlater select creator CHANNEL_ID --remaining
watchlater selection action move --playlist 'Queue - Electronics'
```

or for several destinations:

```bash
watchlater-playlist assign \
    --selection 12 \
    --playlist 'Queue - Electronics' \
    --playlist 'Reference - Repairs'
```

Then complete destination synchronization before building the Watch Later removal plan.

See the [operator guide](index.md) for the full import → review → plan → apply → verify workflow.
