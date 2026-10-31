# Unit 02 — hierarchical-config-layering

## Objective
Implement two-tier hierarchical configuration discovery in `tscribe.config`, resolving a global user base config (`~/.config/tscribe/`) and overlaying an optional project-local override (`.tscribe.toml` in cwd).

## Context
- `src/tscribe/config.py` `_resolve_config_path()` only checks CLI `--config` > `$TSCRIBE_CONFIG` > `./tscribe.yaml`.
- If an operator has a global setup in `~/.config/tscribe/` (pointing to `carnyx`), running `tscribe` from a subproject folder fails unless `$TSCRIBE_CONFIG` is manually exported.
- Sibling precedent (`lrex/src/config.rs`) uses base `~/.config/lufs-recorder/config.toml` + optional cwd `.lufs-recorder.toml` `ProjectOverride`.
- See phase spec: `docs/specs/2026-10-31T0025Z_toml-config-layering-and-unified-cli/SPEC.md`.

## Acceptance criteria
- [ ] If no explicit config path or `$TSCRIBE_CONFIG` is given, base config automatically resolves to `~/.config/tscribe/config.toml` (or `~/.config/tscribe/tscribe.yaml`).
- [ ] If `./.tscribe.toml` or `./tscribe.toml` exists in the current working directory, its keys are deep-merged on top of the base config.
- [ ] Project override allows partial definitions (e.g. only overriding `out_dir` or `chunking.workers` without repeating top-level `mode` or `remote` sections).
- [ ] Passing explicit `--config <path>` bypasses cwd project-override merging unless `--with-project-config` is explicitly set.
- [ ] Unit tests verify:
  1. Base-only loading when cwd has no project file.
  2. Overlay merging when cwd has `.tscribe.toml`.
  3. Precedence: CLI explicit path > `$TSCRIBE_CONFIG` > base + project overlay.

## Interface contract
```python
# In src/tscribe/config.py

PROJECT_CONFIG_FILENAMES: list[str] = [
    ".tscribe.toml",
    "tscribe.toml",
    ".tscribe.yaml",
    "tscribe.yaml",
]

def default_base_config_path() -> Path | None:
    """Return ~/.config/tscribe/config.toml or ~/.config/tscribe/tscribe.yaml if exists."""
    ...

def project_override_path(cwd: Path | None = None) -> Path | None:
    """Check cwd for .tscribe.toml, tscribe.toml, etc."""
    ...

def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    cwd: Path | None = None,
    allow_project_layering: bool = True,
) -> RuntimeConfig:
    """Resolve base config, apply project-level cwd overlay, and apply runtime overrides."""
    ...
```

## Boundaries
- **Touches:**
  - `src/tscribe/config.py`
  - `tests/test_config.py`
- **Does NOT touch:**
  - `src/tscribe/cli/main.py`
  - `src/tscribe/core/`
  - `src/tscribe/server/`

## Verification
```bash
uv run pytest tests/test_config.py -k "test_hierarchical or test_layering"
```
