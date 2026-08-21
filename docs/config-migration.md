# Migrating from env vars to `tscribe.yaml`

`tscribe-class-carnyx` and `tscribe-transcription-tool` are both env-var-only
today (no config file). If you're standing up `tscribe` to replace or sit
alongside one of them, here is every env var either repo reads and its
`tscribe.yaml` equivalent.

**Neither source repo is modified by this migration** — this table exists so
an operator translating an existing deployment doesn't have to read this
repo's source to find the mapping.

| Source | Env var | `tscribe.yaml` field | Notes |
|---|---|---|---|
| carnyx | `TSCRIBE_API_KEY` | *(not yet modeled — Unit 03 server-mode auth)* | Server-mode API key; Unit 01 only models the *client-side* `remote.api_key_env` pointer today. Unit 03 will document the server-side equivalent when it lands. |
| carnyx, transcription-tool | `LITELLM_BASE_URL` | `remote.endpoint` | |
| carnyx, transcription-tool | `LITELLM_API_KEY` | `remote.api_key_env` (name of the var, not the value) | |
| carnyx, transcription-tool | `TRANSCRIPTION_MODEL` | `remote.transcription_model` | |
| transcription-tool | `LITELLM_MODEL` | `summarization.model` | Distinct from `TRANSCRIPTION_MODEL` — this is the summarization LLM, not the whisper model. |
| carnyx | `TSCRIBE_CHUNK_MINUTES` | `chunking.minutes` | |
| carnyx | `TSCRIBE_WORKERS` | `chunking.workers` | |
| carnyx | `TSCRIBE_SNAP_WINDOW_S` | `chunking.snap_window_s` | |
| carnyx | `TSCRIBE_MAX_AUDIO_BYTES` | *(not yet modeled)* | Server-mode upload guard; belongs with Unit 03. |
| carnyx | `TSCRIBE_DOWNLOAD_TIMEOUT_S` | *(not yet modeled)* | Server-mode Drive-pull timeout; belongs with Unit 03. |
| carnyx | `CARNYX_DRIVE_SA_JSON` | *(not yet modeled)* | Server-mode Drive service-account path; belongs with Unit 03. |

## `remote.kind` — a field that didn't exist before

Neither source repo had to make this distinction because carnyx only ever
spoke to a bare LiteLLM endpoint, and transcription-tool's standalone CLI
only ever ran locally. `tscribe` unifies both, plus a third case (client
pointed at *another tscribe server*), so `remote.kind` makes explicit which
of the two `remote:` shapes is meant. **If you're migrating an existing
carnyx or transcription-tool deployment, you want `kind: inference_endpoint`
(the default) — not `tscribe_server`.**

## Precedence while migrating

`tscribe.yaml` fields are the source of truth; CLI flags override them at
invocation time. There is currently no automatic env-var fallback for
individual fields (e.g. `tscribe.yaml` does not read `LITELLM_BASE_URL`
directly) — this table is a *manual* translation guide, not a compatibility
shim. Automatic env-var fallback for a bounded transition period is worth
considering if this friction turns out to matter in practice; not built
in Unit 01.
