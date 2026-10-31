# Unit 03 — cli-transcribe-overrides

## Objective
Add runtime configuration override flags (`--backend`, `--endpoint`, `--model`, `--device`) to `tscribe transcribe` in `tscribe.cli`, passing them directly into `load_config(overrides=...)`.

## Context
- `tscribe.config.load_config()` already supports an `overrides: dict[str, Any]` argument with deep-merge semantics.
- `src/tscribe/cli/main.py`'s `p_transcribe` parser currently only accepts `files` and `--summarize`.
- Operators testing a remote backend or switching between CPU and GPU need to be able to pass flags on the CLI without editing on-disk configuration files.
- See phase spec: `docs/specs/2026-10-31T0025Z_toml-config-layering-and-unified-cli/SPEC.md`.

## Acceptance criteria
- [ ] `tscribe transcribe` accepts `--backend {local,remote}`.
- [ ] `tscribe transcribe` accepts `--endpoint <url>` (sets `remote.endpoint`).
- [ ] `tscribe transcribe` accepts `--model <name>` (sets `remote.transcription_model` if remote, or `local.model_size` if local).
- [ ] `tscribe transcribe` accepts `--device {cpu,cuda}` (sets `local.device`).
- [ ] Provided CLI flags override whatever is configured in the resolved base/project config file.
- [ ] Unset flags leave the config values untouched.
- [ ] Passing an invalid choice (e.g. `--backend invalid`) exits with exit code `2` (`USAGE`) per the CLI contract.
- [ ] Unit tests in `tests/cli/test_main.py` verify that CLI overrides propagate to the client dispatcher.

## Interface contract
```python
# In src/tscribe/cli/main.py

p_transcribe = sub.add_parser("transcribe", help="Transcribe one or more audio files.")
p_transcribe.add_argument("files", nargs="+", help="Audio file paths...")
p_transcribe.add_argument("--summarize", action="store_true", help="Also summarize...")
p_transcribe.add_argument(
    "--backend", choices=["local", "remote"], default=None, help="Override backend mode"
)
p_transcribe.add_argument("--endpoint", type=str, default=None, help="Override remote endpoint URL")
p_transcribe.add_argument("--model", type=str, default=None, help="Override model name or size")
p_transcribe.add_argument(
    "--device", choices=["cpu", "cuda"], default=None, help="Override local compute device"
)


def _build_transcribe_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Translate non-None CLI flags into nested dictionary for load_config."""
    ...
```

## Boundaries — do NOT touch
- **Touches:**
  - `src/tscribe/cli/main.py`
  - `tests/cli/test_main.py`
- **Does NOT touch:**
  - `src/tscribe/core/`
  - `src/tscribe/server/`
  - `src/tscribe/client/`

## Output
- Modified: `src/tscribe/cli/main.py` (`p_transcribe` flags, `_build_transcribe_overrides`).
- Tests added in `tests/cli/test_main.py`, named `test_transcribe_overrides_*` so the Verification command selects them.
- Commit message: `feat(cli): runtime config overrides on tscribe transcribe`

## Verification
```bash
uv run pytest tests/cli/test_main.py -k "test_transcribe_overrides"
```
