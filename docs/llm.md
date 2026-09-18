# LLM provider and prompt configuration

Language-model classification is deliberately a late-stage step. Creator/title rules,
manual decisions and other deterministic triage should reduce the unresolved set before
an LLM is asked to classify anything.

The provider layer uses the OpenAI-compatible Chat Completions wire format. It does not
depend on the OpenAI Python SDK and does not assume that the server is operated by
OpenAI.

## Provider presets

The built-in presets only provide sensible default base URLs; all of them use the same
OpenAI-compatible HTTP client:

| Preset | Default base URL | Intended use |
| --- | --- | --- |
| `ollama` | `http://127.0.0.1:11434/v1` | Ollama's OpenAI-compatible endpoint |
| `vllm` | `http://127.0.0.1:8000/v1` | vLLM OpenAI-compatible server |
| `unsloth` | `http://127.0.0.1:8000/v1` | Unsloth model served through vLLM by default; override for llama-server/Ollama |
| `openrouter` | `https://openrouter.ai/api/v1` | OpenRouter's OpenAI-compatible gateway |
| `generic` | none | Any other remote/local OpenAI-compatible server; `base_url` is required |

Ollama documents `/v1/chat/completions` compatibility. Unsloth's deployment guides use
OpenAI-compatible servers such as vLLM, llama-server, or Ollama, so `unsloth` is a serving
convenience alias rather than a separate protocol. OpenRouter also exposes Chat Completions
at an OpenAI-compatible `/api/v1` base URL.

The implementation intentionally remains small rather than depending on a broad routing
framework such as LiteLLM. For these providers, the common protocol already gives us the
required portability while keeping request/response validation visible and testable.

## Project configuration

Copy `examples/watchlater.example.toml` to `watchlater.toml` and edit the provider/model
names. A minimal local configuration is:

```toml
default_provider = "local"
default_interest_profile = "default"

[providers.local]
preset = "ollama"
model = "qwen3:14b"

[interest_profiles.default]
file = "interests.md"

[playlists]
"Queue - Electronics" = "Electronics, embedded systems and technically deep repair."
```

Paths in an interest profile are relative to the TOML file. Provider `extra` tables can
pass runtime-specific OpenAI-compatible request parameters, for example
`reasoning_effort`, but reserved request fields cannot be overridden there.

If `max_tokens` is omitted from a provider, the client sends a default output limit of
`6000`. This is sized for the structured multi-video classification/annotation batches;
providers can override it per profile when a model needs a tighter or larger ceiling.

### Semantic review categories

The action classifier's free-form topic is useful as evidence, but large-catalogue browsing
works better with a vocabulary that is fixed across LLM batches. Configure broad categories
under `[llm.review_categories]`:

```toml
[llm.review_categories]
"Electronics" = "Electronics, test equipment, embedded systems and hardware engineering."
"Retrocomputing" = "Historic computers, operating systems and unusual architectures."
"Telecoms" = "Telephony, radio, networking, modems and communications."
"Gaming" = "Games and game-related material."
```

The semantic annotator selects exactly one configured category and also emits a short
subject, reusable lower-case tags, a content type and annotation confidence. `Other` and
`Unclear` are reserved fallbacks added automatically.

Annotate the whole snapshot even when some videos already have human/rule decisions:

```bash
watchlater-llm --config watchlater.toml annotate --scope all
```

Or annotate only the unresolved remainder:

```bash
watchlater-llm --config watchlater.toml annotate --scope remaining
```

A selection can be targeted with `--selection SELECTION_ID`. Semantic annotations are
stored separately from action suggestions and never create or supersede a decision.

If you do not want to maintain the broad vocabulary manually, an optional discovery pass
can propose one from an evenly-spaced sample of catalogue metadata and then hold that
vocabulary fixed for the actual annotation batches:

```bash
watchlater-llm --config watchlater.toml annotate \
    --taxonomy discover --taxonomy-sample 250 --max-categories 20
```

A successful discovery is saved immediately, before annotation batches begin, and the
command prints the saved taxonomy ID to stderr. This means the vocabulary survives even
if a later annotation batch fails or the run is interrupted.

List saved taxonomies and inspect one in full:

```bash
watchlater-llm taxonomies
watchlater-llm taxonomy --taxonomy-id 3
```

Reuse exactly that vocabulary on a later run without another taxonomy provider request:

```bash
watchlater-llm --config watchlater.toml annotate \
    --scope all --taxonomy saved --taxonomy-id 3
```

Saved taxonomy provenance includes the source snapshot/selection, provider/model,
discovery hashes, sample count and discovery limits. Reusing a saved taxonomy does not
require the current snapshot to match the snapshot it was discovered from; the origin is
recorded in the annotation-run context.

`--no-store` keeps its existing meaning: a discovery made during a no-store run is not
persisted.

Use `--dry-run` with configured or saved categories to inspect the exact evidence, hashes and
vocabulary without provider calls. With `--taxonomy discover --dry-run`, the command
shows the discovery sample without calling the provider.

Provider `headers` tables can supply safe non-protocol headers. This is useful for
OpenRouter's optional attribution headers:

```toml
[providers.openrouter]
preset = "openrouter"
model = "anthropic/claude-sonnet-4.5"

[providers.openrouter.headers]
HTTP-Referer = "https://github.com/philpem/youtube-watchlater-tidy"
X-Title = "youtube-watchlater-tidy"
```

`Authorization`, `Content-Type`, `Accept`, and `User-Agent` cannot be overridden through
configuration.

### Secrets

Do not put an API key in the TOML file. Configure only the environment-variable name.
The `openrouter` preset defaults to `OPENROUTER_API_KEY`; generic remote profiles can use
any environment variable:

```toml
[providers.remote]
preset = "generic"
base_url = "https://llm.example.invalid/v1"
model = "example-model"
api_key_env = "WATCHLATER_LLM_API_KEY"
```

Then set the secret outside the catalogue/configuration:

```bash
export WATCHLATER_LLM_API_KEY='...'
export OPENROUTER_API_KEY='sk-or-v1-...'
```

Literal config keys named `api_key`, `token`, or `secret` are rejected.

## Structured output modes

Each provider supports one of:

- `json_object` (default): request a JSON object, then validate it in the application;
- `json_schema`: send the application JSON Schema through `response_format`;
- `none`: omit `response_format` for older/minimal OpenAI-compatible servers.

The classifier validates responses itself regardless of server-side structured output
support. Server-side schemas improve generation reliability; they are not treated as a
substitute for application validation. Because OpenRouter forwards to many model/provider
combinations, `json_object` is the conservative default there unless the selected model is
known to support JSON Schema reliably.

## Interest briefs and prompt provenance

The user's interest brief is a plain Markdown/text file and contains only preferences.
It does not need to know about JSON, action enums, or the classification schema. Fixed
application instructions remain separate.

Playlist descriptions are also separate controlled input. The model may prefer one of
those existing playlists or propose a new `Queue - ...` name, but it never creates or
modifies a YouTube playlist itself.

Render the complete effective prompt before using it:

```bash
watchlater-llm --config watchlater.toml prompt
watchlater-llm --config watchlater.toml prompt --interest-profile strict
watchlater-llm --config watchlater.toml prompt --prompt-file another-interest-brief.md
watchlater-llm --config watchlater.toml prompt --hash-only
```

The SHA-256 prompt version covers the fixed application instructions, classification
schema, selected interest brief/guidance and playlist descriptions. Per-video evidence is
not part of this prompt-version hash; classification persistence uses a separate input
hash for that evidence.

## Provider inspection and probe

List resolved provider profiles without exposing secret values:

```bash
watchlater-llm --config watchlater.toml providers
```

Make a small structured-output connectivity test:

```bash
watchlater-llm --config watchlater.toml probe --provider ollama-local
watchlater-llm --config watchlater.toml probe --provider openrouter
```

The probe only verifies the provider/request path. Classification and catalogue writes are
implemented separately so provider/config behavior can be reviewed independently.
