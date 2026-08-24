# Unit 01 — Config Schema, Core Models & Bootstrap-via-bplate

## Objective
Establish the `tscribe` repo skeleton (via `bplate`) and the config-driven foundation — the `tscribe.yaml` schema, its loader/validator, and the core `Mode` / `Backend` / `RuntimeConfig` data models — that every other unit depends on.

## Context
- Spec: `SPEC.md` §3 (Core Nouns), §6 (Language & Runtime Decision)
- Proposal: `tscribe-class-carnyx/docs/proposals/01-generic-tscribe-tool.md` — "what Daniel is actually asking for," items 1-2
- Scaffolding precedent: `lufs-audio/bplate` `docs/units/03-built-in-profiles-and-traits.md` (the `python-cli` profile) and its `AGENTS.md`
- carnyx's current env-var surface to map into config: `TSCRIBE_API_KEY`, `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `TRANSCRIPTION_MODEL`, `TSCRIBE_CHUNK_MINUTES`, `TSCRIBE_WORKERS`, `TSCRIBE_SNAP_WINDOW_S`, `TSCRIBE_MAX_AUDIO_BYTES`, `TSCRIBE_DOWNLOAD_TIMEOUT_S`, `CARNYX_DRIVE_SA_JSON`
- transcription-tool's summarization-specific vars: `LITELLM_MODEL` (default `lightweight`), distinct from `TRANSCRIPTION_MODEL` (the whisper model)

## Status (2026-08-21)
The repo-scaffolding half of this unit is DONE — `bplate new tscribe --profile python-cli --with mise,agent-core,ruff,editor-baseline --author "Daniel Ramirez" --license MIT` ran for real (verified: 32/32 bplate contract checks passed, git initialized with the pre-push hook installed executable). What remains for this unit: the `tscribe.yaml` schema, loader/validator, and `RuntimeConfig` models below — none of that is implemented yet.

## Acceptance criteria
- [x] Repo is scaffolded via `bplate new tscribe --profile python-cli --with mise,agent-core,ruff,github-ci` (executed as `--with mise,agent-core,ruff,editor-baseline`; `github-ci`'s workflow file is handled separately — see PR description — since the pushing token lacks `workflow` scope).
- [ ] `tscribe.yaml` schema defined and documented with at minimum: `mode: server|client`, `backend: local|remote`, `remote.endpoint`, `remote.api_key_env`, `remote.transcription_model`, `local.model_size`, `local.device`, `local.compute_type`, `chunking.minutes`, `chunking.workers`, `chunking.snap_window_s`, `summarization.model`, `summarization.prompt`.
- [ ] A loader (`tscribe.config`) parses `tscribe.yaml`, validates it (missing/invalid fields fail loudly with a specific message, never a bare stack trace), and produces a typed `RuntimeConfig` object.
- [ ] Every one of today's carnyx and transcription-tool env vars has an explicit config-file equivalent, documented in a mapping table.
- [ ] Config loading has a documented precedence order: CLI flag > env var > config file > default.
- [ ] Unit tests: a valid config loads correctly; each documented invalid config fails with a specific, distinguishable error.

## Interface contract
```python
class Mode(str, Enum):
    SERVER = "server"
    CLIENT = "client"


class Backend(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


@dataclass
class RemoteConfig:
    endpoint: str  # name of the env var holding the secret
    api_key_env: str
    transcription_model: str | None = None


@dataclass
class LocalConfig:
    model_size: str = "small"  # tiny|base|small|medium|large
    device: str = "cpu"
    compute_type: str = "int8"


@dataclass
class ChunkingConfig:
    minutes: int = 15
    workers: int = 2
    snap_window_s: int = 10


@dataclass
class SummarizationConfig:
    model: str = "lightweight"
    prompt: str | None = None


@dataclass
class RuntimeConfig:
    mode: Mode
    backend: Backend
    remote: RemoteConfig | None
    local: LocalConfig
    chunking: ChunkingConfig
    summarization: SummarizationConfig


def load_config(path: str | None = None) -> RuntimeConfig: ...
```
Every later unit that needs configuration imports `RuntimeConfig` from here.

## Boundaries — do NOT touch
- Does not implement the transcription logic itself (Unit 02).
- Does not implement the HTTP server or its routes (Unit 03).
- Does not implement the CLI's argument parsing or subcommands (Unit 07).

## Output
- `pyproject.toml`, `mise.toml`, `AGENTS.md`, `ruff.toml`, `.github/workflows/` (from the bplate scaffold — DONE)
- `src/tscribe/config.py`, `tscribe.example.yaml`, `docs/config-migration.md`, `tests/test_config.py` — NOT YET DONE
- Commit style: `feat(config): implement tscribe.yaml schema + loader`

## Verification
- `pytest tests/test_config.py`
- A smoke entrypoint runs without a config file present and gives a clear message rather than crashing