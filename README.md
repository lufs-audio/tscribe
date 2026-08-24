# tscribe

Agent-first transcription primitive — one config-driven tool that turns
audio into a **verified** transcript, locally or against a remote GPU
backend, replacing three hand-built single-purpose repos
(`tscribe-class-carnyx`, `tscribe-transcription-tool`, `tscribe-capture-rig`)
with one LUFS Primitive.

Full architecture, rationale, and the unit-by-unit build spec live in
[`SPEC.md`](./SPEC.md) and [`docs/units/`](./docs/units/) — this file is a
front door, not a duplicate. If something here and `SPEC.md` disagree,
`SPEC.md` wins.

## Install

```bash
pip install -e .              # client + local backend only (light path)
pip install -e ".[server]"    # + FastAPI server mode, Google Drive integration
pip install -e ".[local]"     # + faster-whisper (needed to actually run backend: local)
pip install -e ".[record]"    # + mic recording (tscribe record / tscribe devices)
pip install -e ".[all]"       # everything at once
```

The base install is deliberately light (`pyyaml` + `requests` only) so a
`mode: client` / `backend: remote` deployment — the common case for a machine
that only ever talks to a GPU host or another `tscribe` server — never pulls
in FastAPI, Google API clients, or ML dependencies it doesn't need. See
`pyproject.toml`'s `[project.optional-dependencies]` for the exact split.

## Terminal monitor (`tui/`)

A separate Rust crate, `tui/`, provides `tscribe-tui` — a read-only terminal
monitor for a job in progress (`GET /jobs/{id}`: status, elapsed time, and
the verification report). It isn't part of this Python package or its `uv`
build; see `tui/README.md` for why, and how to build/run it. Follows the
cross-tool TUI style guide locked for LUFS's terminal tools
(`lufs-audio/bplate` → `references/studies/tui-style-directions/`), the same
one `lufs-audio/lrex`'s own `tui` follows — one interaction shape, one set of
three themes, shared across the family. Per `SPEC.md` §7, tscribe's own spec
explicitly deferred TUI design to that guide rather than designing one here.

## Quickstart

```bash
tscribe bootstrap --backend local --selftest
# writes tscribe.yaml, checks ffmpeg/ffprobe, runs a tiny synthetic job
# through the real pipeline, and reports pass/fail.

tscribe transcribe path/to/audio.wav
# verified transcript to stdout, or a CONTRACT_VIOLATED exit + retained
# evidence if it doesn't pass (see "Verified, not just exited 0" below).

tscribe transcribe path/to/audio.wav --summarize
tscribe summarize --obsidian < transcript.md
tscribe devices && tscribe record --seconds 30 --out take.wav
```

## Mode × Backend

One clone, one entrypoint — `tscribe.yaml`'s `mode`/`backend` decide what it
does at runtime:

| `mode` | `backend` | `remote.kind` | What runs |
|---|---|---|---|
| `client` | `local` | — | Transcription runs in this process via `faster-whisper`. No network. |
| `client` | `remote` | `inference_endpoint` | Transcription runs in this process; only the whisper call goes to a bare LiteLLM/whisper-compatible endpoint (this is what `tscribe-class-carnyx` does today). |
| `client` | `remote` | `tscribe_server` | This process is a thin HTTP client of a *real tscribe server* (`POST /jobs`, `GET /jobs/{id}`) — no local transcription at all. |
| `server` | `local` or `remote` | — | Runs the FastAPI job-queue server (`tscribe serve`); the backend fields describe how *that server* transcribes. |

Migrating an existing carnyx or transcription-tool deployment onto
`tscribe.yaml`? See [`docs/config-migration.md`](./docs/config-migration.md)
for the full env-var mapping table.

## Verified, not just exited 0

Every transcription is checked against a contract (completeness, coverage,
reassembly integrity, speech-rate band) before it's trusted. On failure, the
**unverified transcript and the full report are retained, never discarded** —
`tscribe transcribe` exits `CONTRACT_VIOLATED` (5) but still prints what it
has, so a misfiring check costs a re-run of the checker, not the whole job.
See `docs/units/02-transcription-core.md` for the contract itself and
`src/tscribe/core/class_pipeline/verify.py` for the checks.

The same principle extends to `tscribe capture` (a `not_silence_floor` check
catches a muted/unplugged mic that would otherwise "successfully" record
digital silence for hours) and `tscribe bootstrap --selftest` (reports
exactly which part of a synthetic end-to-end job it could and couldn't
actually exercise in the current environment, rather than a blanket
pass/fail).

## CLI surface

```
tscribe [--config PATH] [--json] <command> ...

  serve                             mode: server — run the FastAPI job queue
  transcribe <files...> [--summarize]
  record [--seconds N] [--device N] [--out PATH]
  devices
  summarize [file] [--obsidian]     reads stdin if no file given
  capture start [--dry-run] [--duration N] [--list]
  capture verify <file> [--final] [--expected-seconds N]
  capture finalize <session_dir> [--out PATH]
  bootstrap [--mode server|client] [--backend local|remote] [--endpoint URL]
            [--force] [--selftest] [--dry-run] [--write-systemd-unit]
  jobs status <id>                  poll a remote tscribe server
```

Every command supports `--json`, emitting exactly one of:

```json
{"status": "success", "data": {...}}
{"status": "error", "code": 3, "message": "..."}
```

`code` in an error envelope *is* the process's exit code — the two are never
allowed to disagree. Full exit-code table:

| Code | Meaning |
|---|---|
| 0 | SUCCESS |
| 2 | USAGE — bad arguments, missing input, or an unmet environment precondition (no ffmpeg, no matching capture device) |
| 3 | CONFIG_INVALID — `tscribe.yaml` missing or malformed |
| 4 | BACKEND_UNREACHABLE — a configured remote endpoint didn't respond |
| 5 | CONTRACT_VIOLATED — the verification contract rejected the result (evidence retained) |
| 6 | JOB_NOT_FOUND — `jobs status` for an unknown id |
| 7 | INTERRUPTED — declined a prompt, or a real interrupt |

See [`docs/units/07-cli-surface-and-exit-codes.md`](./docs/units/07-cli-surface-and-exit-codes.md)
for the full contract.

## Development

```bash
mise run test    # pytest, PYTHONPATH=src
mise run check   # ruff check
mise run fmt     # ruff format
```

`CHANGELOG.md` tracks notable changes per unit. `docs/units/` holds the
per-unit spec each was built against, including what's execution-tested vs.
code-reviewed-only in this environment (real PipeWire capture, the Windows
capture path, and real faster-whisper inference all need hardware/weights
this repo's own CI doesn't have — see each unit's own honesty notes).

For instructions on executing a full hardware capture and microphone testing pass,
see [`docs/hardware-capture-test-plan.md`](./docs/hardware-capture-test-plan.md).

## Where this came from

`tscribe` unifies three previously-separate repos (see `SPEC.md` §1-§2 for
the full history):

- **`tscribe-class-carnyx`** (still live production code, untouched by this
  repo) — the FastAPI server + job lifecycle + Google Drive integration this
  repo's `mode: server` ports (Unit 03).
- **`tscribe-transcription-tool`** — the `Transcriber` / chunk-verify-reassemble
  pipeline / summarizer this repo's `tscribe.core` ports (Unit 02), plus the
  standalone personal CLI `mode: client` restores (Unit 04).
- **`tscribe-capture-rig`** — the verified segmented-recording rig
  `tscribe capture` ports (Unit 05).

A future `lufs-audio/kb` product doc suite (`docs/product/tscribe/`) is a
deliberate follow-on once the tool has shipped with real usage to narrate —
not written yet, on purpose (see `docs/units/08-documentation-ci-and-release-readiness.md`).

## License

MIT — see [`LICENSE`](./LICENSE).
