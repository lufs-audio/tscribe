# Unit 04 — unified-tui-monitor-command

## Objective
Unify the terminal monitor experience under the main `tscribe` CLI by adding `tscribe monitor <job_id>` (and alias `tscribe jobs monitor <job_id>`), automatically resolving host, port, and authentication from the active configuration and executing the `tscribe-tui` binary.

## Context
- `tscribe-tui` is a standalone Rust binary built with `cargo` inside `tui/`.
- Currently, running the monitor requires invoking `tscribe-tui --port 6390 --job-id <id> --host carnyx --api-key ...`, forcing the operator to duplicate information already stored in `tscribe.yaml` / `tscribe.toml`.
- The user should only ever have to run `tscribe monitor <job_id>`.
- Resolves GitHub Issue #4 (`feat(tui): Interactive transcription queue and job dispatcher`).
- See phase spec: `docs/specs/2026-10-31T0025Z_toml-config-layering-and-unified-cli/SPEC.md`.

## Acceptance criteria
- [ ] `tscribe monitor <id>` and `tscribe jobs monitor <id>` are recognized commands in `src/tscribe/cli/main.py`.
- [ ] If `cfg.remote` is configured (`endpoint="http://host:port"`):
  - `--host` defaults to the URL host (e.g. `carnyx` or `127.0.0.1`).
  - `--port` defaults to the URL port (e.g. `6390` or `8000`).
  - `--api-key` is sourced from `os.environ[cfg.remote.api_key_env]`.
- [ ] Flags `--host`, `--port`, `--theme` (`lufs|catppuccin|mono`) can still be overridden explicitly on the CLI.
- [ ] Binary discovery order:
  1. `shutil.which("tscribe-tui")` on `$PATH`.
  2. Relative build path `Path(__file__).parents[2] / "tui/target/release/tscribe-tui"`.
  3. If unbuilt/missing, exits with `USAGE` (code 2) and actionable instructions to build it (`cargo build --release` in `tui/`).
- [ ] Replaces process with `os.execvp` or invokes `subprocess.run` to hand full TTY/terminal control to Ratatui.
- [ ] Unit tests verify argument construction, binary resolution, and missing-binary error handling.

## Interface contract
```python
# In src/tscribe/cli/main.py

p_monitor = sub.add_parser(
    "monitor", help="Watch a transcription job live in the terminal monitor (TUI)."
)
p_monitor.add_argument("id", help="Job ID returned from POST /jobs or POST /jobs/upload")
p_monitor.add_argument(
    "--host", default=None, help="Server host (default: derived from remote.endpoint)"
)
p_monitor.add_argument(
    "--port", type=int, default=None, help="Server port (default: derived from remote.endpoint)"
)
p_monitor.add_argument(
    "--theme", choices=["lufs", "catppuccin", "mono"], default="lufs", help="TUI theme palette"
)

# In src/tscribe/cli/commands.py


def cmd_monitor(
    cfg: RuntimeConfig, *, job_id: str, host: str | None, port: int | None, theme: str
) -> CliResponse:
    """Resolve endpoint parameters, locate tscribe-tui, and launch monitor."""
    ...
```

## Boundaries — do NOT touch
- **Touches:**
  - `src/tscribe/cli/main.py`
  - `src/tscribe/cli/commands.py`
  - `tests/cli/test_main.py`
  - `tui/README.md`
- **Does NOT touch:**
  - `tui/src/` (Rust crate source is unchanged; interface contract preserved)
  - `src/tscribe/core/`
  - `src/tscribe/server/`

## Output
- Modified: `src/tscribe/cli/main.py` (`monitor` and `jobs monitor` subparsers), `src/tscribe/cli/commands.py` (`cmd_monitor`).
- Docs: `tui/README.md` — document `tscribe monitor <id>` as the supported entry point.
- Tests added in `tests/cli/test_main.py`, named `test_monitor_command_*` so the Verification command selects them.
- Commit message: `feat(cli): tscribe monitor <id> resolves config and launches the TUI`

## Verification
```bash
uv run pytest tests/cli/test_main.py -k "test_monitor_command"
```
