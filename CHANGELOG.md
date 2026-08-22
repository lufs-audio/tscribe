# Changelog

All notable changes to `tscribe` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This repo has not been merged to `main` or released yet — everything below
landed on the `units/01-08-implementation` feature branch (PR #1) per
`SPEC.md`'s unit-by-unit build order and is grouped under `[Unreleased]`
until Daniel decides on a first version tag.

## [Unreleased]

### Added

- **Scaffold** — `bplate new tscribe --profile python-cli --with mise,agent-core,ruff,editor-baseline`.
  `SPEC.md` and `docs/units/01` through `08` (the full 8-unit build spec).
- **Unit 01 — Config schema & core models** (`src/tscribe/config.py`) —
  `Mode`/`Backend`/`RemoteKind`/`RuntimeConfig` and nested dataclasses; a
  pure `parse_config_dict` plus file-loading `load_config` with documented
  precedence (CLI override > `TSCRIBE_CONFIG` env var > `tscribe.yaml` >
  default). `tscribe.example.yaml` and `docs/config-migration.md` (env-var
  mapping for anyone translating an existing carnyx/transcription-tool
  deployment). Resolved the "remote = tscribe server vs. remote = bare
  inference endpoint" ambiguity via an explicit `RemoteKind` field.
- **Unit 02 — Transcription core** (`src/tscribe/core/`) — `Transcriber`
  (remote-first via a bare LiteLLM/whisper-compatible endpoint, automatic
  local `faster-whisper` fallback), `class_pipeline` (silence-aware chunking,
  parallel transcription, lossless reassembly), and `summarizer` (LiteLLM
  summarization with optional Obsidian-frontmatter output), ported from
  `tscribe-transcription-tool`. Verification (`Report`/`Check`/
  `ContractViolation`) checks completeness, coverage, reassembly word-count
  integrity, and a speech-rate band; on failure the unverified transcript and
  full report are retained, never discarded (the load-bearing lesson from
  carnyx's 2026-08-06 incident).
- **Unit 03 — Server mode (HTTP API)** (`src/tscribe/server/`) — the FastAPI
  app (`create_app(cfg)` factory), job lifecycle (`submit`/`get_job`, one
  daemon thread per job, `queued -> running -> done|failed`), and Google
  Drive integration (service-account download, public-URL pull, resumable
  write-back, non-destructive reparenting), ported from
  `tscribe-class-carnyx` with the `/healthz` `/version` `/jobs`
  `/jobs/upload` `/jobs/{id}` contract preserved byte-for-byte. `ServerConfig`
  added to Unit 01's schema.
- **Unit 04 — Client mode** (`src/tscribe/client/`) — `LocalClient`
  (in-process, no network), `RemoteServerClient` (HTTP against Unit 03's
  `/jobs` contract), and `RemoteInferenceClient` (in-process pipeline, only
  the whisper call goes to a bare endpoint), selected by `make_client(cfg)`.
  `recorder.py` restores mic recording/device enumeration
  (`tscribe record`/`tscribe devices`) from `tscribe-transcription-tool`.
- **Unit 05 — Capture integration** (`src/tscribe/capture/`) — a
  cross-platform port of `tscribe-capture-rig`'s verified segmented-recording
  rig: `verify_capture` (exists/decodes/sample-rate/channels/duration-band/
  not-silence-floor checks, thresholds now config-driven via the new
  `CaptureConfig` section), `capture_start` (PipeWire on Linux, DirectShow on
  Windows, one Python implementation replacing the source rig's separate
  `.sh`/`.bat` scripts), and `capture_finalize` (lossless concat + final
  verify against the *measured* sum of segment durations).
- **Unit 06 — Bootstrap command** (`src/tscribe/bootstrap.py`) —
  `run_bootstrap`: dependency check (ffmpeg/ffprobe/pyyaml/requests),
  idempotent `tscribe.yaml` writing (with a generated API key for a fresh
  server-mode setup), an optional `--selftest` synthetic end-to-end job
  through Unit 02, and an optional systemd unit scaffold (written to disk
  only — never installed or enabled). Exposed as typed exceptions
  (`DependencyMissingError`, `ConfigInvalidError`, `BackendUnreachableError`,
  `SelftestContractViolationError`, `InterruptedBootstrapError`) rather than
  process exit codes, so it stayed independently testable before Unit 07
  existed. No prior `lsbx-cli` bootstrap pattern existed to consult at
  `lufs-audio/lufs-sandbox-provider` at the time this was built — this is the
  first implementation of this shape in the fleet.
- **Unit 07 — CLI surface, JSON envelope & exit codes** (`src/tscribe/cli/`)
  — the single `tscribe` entrypoint. Subcommands: `serve`, `transcribe`,
  `record`, `devices`, `summarize`, `capture start|verify|finalize`,
  `bootstrap`, `jobs status`. Every subcommand supports `--json`; exit codes
  are `0 SUCCESS / 2 USAGE / 3 CONFIG_INVALID / 4 BACKEND_UNREACHABLE /
  5 CONTRACT_VIOLATED / 6 JOB_NOT_FOUND / 7 INTERRUPTED`, with the `--json`
  error envelope's `code` field always matching the process's actual exit
  code. Added the missing `RemoteInferenceClient`/`remote_inference.py` gap
  found while integrating Unit 04. Batch transcription's non-WAV
  auto-conversion (via `ffmpeg`) lives here, not in Unit 04, per that unit's
  own boundary.
- **Unit 08 — Documentation, CI & release readiness** — this changelog, real
  `README.md` and `AGENTS.md` (replacing the bplate scaffold boilerplate),
  and a fleet-adapted `.github/workflows/ci.yml` (handed to Daniel to commit
  directly — see the PR description; the GitHub App token backing this
  integration lacks `workflow` scope).
- `pyproject.toml` dependencies split into optional extras (`server`,
  `local`, `record`, `all`, `dev`) so a client-only/local-only install stays
  light — the base install remains just `pyyaml` + `requests`.

### Fixed

- `bootstrap.py`'s `_default_confirm` only caught `EOFError` for "no stdin to
  read"; a captured-output test runner (and some non-interactive contexts)
  raise `OSError` instead ("reading from stdin while output is captured"),
  which fell through uncaught and surfaced as a generic failure instead of
  the documented `InterruptedBootstrapError` -> `INTERRUPTED` (7) exit code.
  Found while writing Unit 07's end-to-end CLI tests; now catches both.
- 41 `ruff` line-length findings across several units' files, cleaned up via
  `ruff format .` rather than by hand.

### Known, deliberately not fixed

- `ruff`'s `UP042` (Mode/Backend/RemoteKind could be `StrEnum`) and `N818`
  (`ContractViolation` could be `ContractViolationError`) are suppressed in
  `ruff.toml` with rationale in comments there — both are pre-existing,
  widely-referenced choices from earlier units; fixing either now is a wide,
  purely-cosmetic change for no functional gain.
- `tscribe.core.summarizer` has no dedicated test file (`test_summarizer.py`)
  in the ported suite, unlike every other Unit 02 module — flagged during
  Unit 02 verification, not yet closed.
- Real PipeWire capture, the Windows/DirectShow capture path, real
  `faster-whisper` inference, and `lsbx-cli`'s bootstrap pattern (the repo
  does not exist yet under `lufs-audio`) are all either code-reviewed-only or
  entirely unavailable in this environment — see each unit's own honesty
  notes in `docs/units/` and the implementation report
  (`docs/units/09-implementation-report.md`) for the full accounting.
