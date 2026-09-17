# Documentation

Start with the repository-level [`USER_GUIDE.md`](../USER_GUIDE.md). It is the primary operator HOWTO and is organized around the complete lifecycle:

1. export/import Watch Later;
2. choose a manual/rule/LLM review workflow;
3. record local decisions;
4. plan external changes;
5. dry-run them;
6. apply playlist changes / Watch Later removals;
7. re-export and verify.

Focused references:

- [`review.md`](review.md) — HTML manual review, explicit override export/import, and what happens after import.
- [`playlist-sync.md`](playlist-sync.md) — single/multi-destination playlist assignment, planning, API/Playwright execution and checkpoints.
- [`playlist-browser.md`](playlist-browser.md) — browser destination-playlist executor details.
- [`watchlater-removal.md`](watchlater-removal.md) — selective Watch Later removal planning, move gating and destructive execution safeguards.
- [`dearrow.md`](dearrow.md) — DeArrow enrichment, trust rules, privacy and attribution.
- [`llm.md`](llm.md) — LLM provider configuration and interest profiles.
- [`llm-classification.md`](llm-classification.md) — classification evidence, validation, history and caching.
- [`rich-metadata.md`](rich-metadata.md) — selective yt-dlp description/metadata enrichment.
- [`transcripts.md`](transcripts.md) — selective caption acquisition and transcript-aware refinement.

The user guide describes the workflow; focused docs describe one subsystem in more depth. `COMMAND --help` remains the definitive option reference for the installed version.
