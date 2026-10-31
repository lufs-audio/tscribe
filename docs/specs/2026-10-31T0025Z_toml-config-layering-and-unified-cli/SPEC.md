# Phase Spec: TOML Configuration, Project Layering, CLI Overrides, and Unified TUI

- **Phase ID:** `2026-10-31T0025Z_toml-config-layering-and-unified-cli`
- **Target Repo:** `lufs-audio/tscribe`
- **Related Issues:** #4 (TUI dispatcher), aligns with LUFS cross-tool UX standards

---

## 1. Problem Statement

`tscribe` was built as a clean, single-point transcription primitive replacing three fragmented repositories. While the core transcription pipeline and verification contract are solid, several ergonomic gaps exist when operating in real recording environments alongside sibling tools:

1. **Config Format Inconsistency:** Sibling LUFS audio tools (such as `lrex` / `lufs-recorder`) standardized on TOML (`config.toml`, `.lufs-recorder.toml`), while `tscribe` uses YAML (`tscribe.yaml`). Developers and operators managing multi-tool setups must maintain disparate config syntaxes.
2. **Lack of Project-Level Layering:** `tscribe` currently operates on an "all-or-nothing" config resolution (`--config` > `$TSCRIBE_CONFIG` > `./tscribe.yaml`). It lacks automatic fallback to standard user directories (`~/.config/tscribe/`) and does not layer project-local directory overrides on top of a global base config.
3. **No CLI Invocation Overrides:** `tscribe transcribe` requires config-backed defaults and does not allow ad-hoc flag overrides (e.g. `--backend remote`, `--device cpu`, `--endpoint <url>`) at runtime.
4. **Detached TUI Binary:** The terminal monitor (`tscribe-tui`) is a detached Rust binary requiring manual invocation with duplicate arguments (`--host`, `--port`, `--job-id`, `--api-key`). Users must memorize and switch between two separate CLI commands rather than driving the monitor directly through `tscribe`.

---

## 2. Goals & Constraints

### Goals
- **First-class TOML Support:** Enable `.toml` config files (`tscribe.toml`, `.tscribe.toml`) using Python 3.11+'s built-in `tomllib` (zero external dependencies). Maintain full backwards compatibility with legacy `.yaml`.
- **Project-Level Hierarchical Configs:** Establish a two-tier configuration system:
  1. Base config loaded from `~/.config/tscribe/config.toml` (or `$TSCRIBE_CONFIG`).
  2. Optional project-local `.tscribe.toml` in cwd layered over the base config.
- **Runtime Flag Overrides:** Allow flags on `tscribe transcribe` (`--backend`, `--endpoint`, `--model`, `--device`) to override configuration settings in place for the current invocation.
- **Unified CLI TUI Experience:** Add `tscribe monitor <job_id>` (and alias `tscribe jobs monitor <job_id>`) that automatically derives host, port, and auth from the active config and delegates to the `tscribe-tui` binary.

### Constraints
- **Zero New Dependencies:** TOML reading must utilize Python's standard library `tomllib`.
- **Strict Backwards Compatibility:** Existing `tscribe.yaml` setups, environment variable lookups (`$TSCRIBE_CONFIG`, `$TSCRIBE_API_KEY`), and exit codes (`0..7`) must continue functioning without breakage.
- **Preserve Independent TUI Build:** The Rust crate in `tui/` remains built with `cargo` and independent of Python packaging. The unified CLI merely orchestrates invocation.

---

## 3. Ecosystem References

- **`lufs-audio/lrex` (LUFS Audio Recorder):** Precedent for TOML-first configuration and two-tier project layering (`~/.config/lufs-recorder/config.toml` base + cwd `.lufs-recorder.toml` `ProjectOverride`). See `lrex/src/config.rs`.
- **`carnyx` (Remote GPU Transcription Server):** Live server running `tscribe-class-carnyx` on port 6390, authenticated via `TSCRIBE_API_KEY`, implementing the byte-for-byte `/jobs` and `/jobs/upload` API contract.
- **Cross-Tool TUI Style Guide:** Standards locked in `lufs-audio/bplate` (`references/studies/tui-style-directions/`), which `tscribe-tui` follows with ratatui themes (`lufs`, `catppuccin`, `mono`).

---

## 4. Architectural Approach

### 4.1 TOML Configuration
Python 3.11+ provides `tomllib.loads()`. `tscribe.config` will detect file extensions:
- `.toml`: Parsed via `tomllib.loads()`.
- `.yaml` / `.yml`: Parsed via `yaml.safe_load()`.
- When searching default locations without an explicit extension, `config.toml` / `tscribe.toml` takes precedence over `tscribe.yaml`.

### 4.2 Hierarchical Resolution
The config discovery order becomes:
1. Explicit `--config <path>` (ignores cwd overrides unless explicitly requested).
2. `$TSCRIBE_CONFIG` if set.
3. User default base: `~/.config/tscribe/config.toml` (or `~/.config/tscribe/tscribe.yaml`).
4. Current directory project override: If `./.tscribe.toml` or `./tscribe.toml` exists, its fields are deep-merged on top of the base configuration.

### 4.3 CLI Overrides on Subcommands
`argparse` options added to `p_transcribe`:
- `--backend {local,remote}`
- `--endpoint <url>`
- `--model <name>`
- `--device {cpu,cuda}`

Parsed values are transformed into an `overrides` dictionary and passed into `load_config(path, overrides=...)`.

### 4.4 Unified TUI Dispatcher
Add command:
```bash
tscribe monitor <job_id> [--host <host>] [--port <port>] [--theme {lufs,catppuccin,mono}]
```
When invoked:
1. `cfg` is resolved. If `cfg.remote` exists and is `tscribe_server`, default `--host` and `--port` are parsed from `cfg.remote.endpoint` (e.g. `http://carnyx:6390` -> host: `carnyx`, port: `6390`).
2. API key is resolved from `os.environ[cfg.remote.api_key_env]`.
3. Locate binary:
   - Checks `shutil.which("tscribe-tui")`.
   - If not in PATH, checks relative `tui/target/release/tscribe-tui`.
   - If missing, exits with actionable error: `"tscribe-tui binary not found. Build it with 'cd tui && cargo build --release' or place on PATH."`
4. Replaces or spawns process via `os.execvp` or `subprocess.run`, forwarding terminal control to ratatui.

---

## 5. Done Criteria

- [ ] `tscribe.toml` parses identically to `tscribe.yaml` for both client and server modes.
- [ ] User base config in `~/.config/tscribe/config.toml` automatically discovered without setting `$TSCRIBE_CONFIG`.
- [ ] Project-local `./.tscribe.toml` cleanly overlays settings on top of base config.
- [ ] `tscribe transcribe --backend remote ...` overrides file settings at runtime.
- [ ] `tscribe monitor <id>` seamlessly launches the Rust TUI with host/port/key pre-filled from config.
- [ ] Existing 245 unit and integration tests continue to pass; new tests added for TOML, layering, and CLI flags.

---

## 6. Units of Work

- **Unit 01 (`units/01-toml-config-support.md`):** TOML parser in `config.py` using `tomllib`, supporting both `.toml` and `.yaml`.
- **Unit 02 (`units/02-hierarchical-config-layering.md`):** Base `~/.config/tscribe/` resolution and project-local `./.tscribe.toml` layering.
- **Unit 03 (`units/03-cli-transcribe-overrides.md`):** Runtime CLI flags (`--backend`, `--endpoint`, `--model`, `--device`) wired to `load_config(overrides=...)`.
- **Unit 04 (`units/04-unified-tui-monitor-command.md`):** `tscribe monitor <id>` command auto-resolving parameters and dispatching to `tscribe-tui`.
