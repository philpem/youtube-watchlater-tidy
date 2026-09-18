# youtube-watchlater-tidy user guide

The canonical operator guide now lives at [`docs/index.md`](docs/index.md), where it is also the
landing page for the MkDocs documentation site.

Published documentation: <https://philpem.github.io/youtube-watchlater-tidy/>

On GitHub, the Markdown file is directly readable without building anything.

For a local wiki-style site:

```bash
pip install -e '.[docs]'
mkdocs serve
```

Then open the local URL printed by MkDocs.

The guide covers the complete workflow:

1. export and import Watch Later;
2. repair/recover metadata as needed;
3. choose manual cohort, HTML review, saved-rule, or LLM-assisted workflows;
4. record local decisions;
5. plan and dry-run playlist/removal operations;
6. explicitly apply changes to YouTube;
7. re-export and import again to verify the result.

Use `COMMAND --help` as the definitive option reference for the installed version.
