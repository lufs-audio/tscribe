# Unit 01 — toml-config-support

## Objective
Enable native TOML configuration support in `tscribe.config` using Python's standard library `tomllib`, parsing both `.toml` and `.yaml` config files interchangeably with zero external dependencies.

## Context
- `src/tscribe/config.py` currently uses `yaml.safe_load(raw_text)` inside `load_config()`.
- Python 3.11+ provides `tomllib` as a standard library module (`tomllib.loads()`).
- Sibling projects like `lrex` standardized on TOML; `tscribe` needs to accept `tscribe.toml` or `config.toml` seamlessly.
- See phase spec: `docs/specs/2026-10-31T0025Z_toml-config-layering-and-unified-cli/SPEC.md`.

## Acceptance criteria
- [ ] `load_config(path)` correctly parses `.toml` files into `RuntimeConfig` with the same dataclass validation as YAML.
- [ ] Unknown fields or invalid syntax in `.toml` raise `ConfigError` with clear, actionable error messages.
- [ ] `_resolve_config_path()` checks for `tscribe.toml` prior to `tscribe.yaml` when no explicit path is given.
- [ ] Legacy `.yaml` and `.yml` configs continue to parse identically with full backwards compatibility.
- [ ] Unit tests in `tests/test_config.py` verify identical dataclass construction from equivalent YAML and TOML fixtures.

## Interface contract
```python
# In src/tscribe/config.py

import tomllib  # Python 3.11+ stdlib

DEFAULT_CONFIG_FILENAMES: list[str] = [
    "tscribe.toml",
    "tscribe.yaml",
]


def load_config_text(raw_text: str, *, filename_hint: str = "tscribe.toml") -> dict[str, Any]:
    """Parse raw text based on file extension (.toml vs .yaml/.yml).
    Raises ConfigError on syntax errors."""
    ...


def parse_config_dict(data: dict[str, Any]) -> RuntimeConfig:
    """Unchanged: validates raw dict into typed RuntimeConfig."""
    ...
```

## Boundaries — do NOT touch
- **Touches:**
  - `src/tscribe/config.py`
  - `tests/test_config.py`
- **Does NOT touch:**
  - `src/tscribe/cli/main.py`
  - `src/tscribe/server/`
  - `src/tscribe/core/`

## Output
- Modified: `src/tscribe/config.py` (`load_config_text`, `DEFAULT_CONFIG_FILENAMES`, config-path resolution).
- Tests added in `tests/test_config.py`: equivalent YAML and TOML fixtures must construct an identical `RuntimeConfig`.
- Commit message: `feat(config): native TOML config support via stdlib tomllib`

## Verification
```bash
uv run pytest tests/test_config.py
```
