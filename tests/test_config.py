"""Unit 01 acceptance tests: config schema, loader, validation.

Run with: pytest tests/test_config.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tscribe.config import (
    Backend,
    ConfigError,
    Mode,
    RemoteKind,
    _resolve_config_path,
    config_to_dict,
    default_base_config_path,
    load_config,
    load_config_text,
    parse_config_dict,
    project_override_path,
)

# ---------------------------------------------------------------------------
# parse_config_dict — pure validation logic, no filesystem involved
# ---------------------------------------------------------------------------


def test_minimal_local_config_loads_with_defaults():
    cfg = parse_config_dict({"mode": "client", "backend": "local"})
    assert cfg.mode == Mode.CLIENT
    assert cfg.backend == Backend.LOCAL
    assert cfg.remote is None
    assert cfg.local.model_size == "small"
    assert cfg.local.device == "cpu"
    assert cfg.chunking.minutes == 15
    assert cfg.chunking.workers == 2
    assert cfg.summarization.model == "lightweight"


def test_full_remote_server_config_loads():
    cfg = parse_config_dict(
        {
            "mode": "server",
            "backend": "remote",
            "remote": {
                "endpoint": "https://tscribe.example.com",
                "api_key_env": "TSCRIBE_API_KEY",
                "kind": "tscribe_server",
            },
        }
    )
    assert cfg.mode == Mode.SERVER
    assert cfg.backend == Backend.REMOTE
    assert cfg.remote is not None
    assert cfg.remote.endpoint == "https://tscribe.example.com"
    assert cfg.remote.api_key_env == "TSCRIBE_API_KEY"
    assert cfg.remote.kind == RemoteKind.TSCRIBE_SERVER


def test_remote_inference_endpoint_is_the_default_kind():
    """Matches 'carnyx today': backend remote with no explicit kind should
    mean 'bare inference endpoint', not 'another tscribe server' — that was
    the actual production shape being ported, so it's the safer default."""
    cfg = parse_config_dict(
        {
            "mode": "client",
            "backend": "remote",
            "remote": {
                "endpoint": "http://100.89.168.11:6280/v1",
                "api_key_env": "LITELLM_API_KEY",
                "transcription_model": "Systran/faster-distil-whisper-large-v3",
            },
        }
    )
    assert cfg.remote.kind == RemoteKind.INFERENCE_ENDPOINT
    assert cfg.remote.transcription_model == "Systran/faster-distil-whisper-large-v3"


def test_missing_mode_raises_specific_error():
    with pytest.raises(ConfigError, match="mode"):
        parse_config_dict({"backend": "local"})


def test_missing_backend_raises_specific_error():
    with pytest.raises(ConfigError, match="backend"):
        parse_config_dict({"mode": "client"})


def test_invalid_mode_value_raises_specific_error():
    with pytest.raises(ConfigError, match="mode"):
        parse_config_dict({"mode": "bogus", "backend": "local"})


def test_invalid_backend_value_raises_specific_error():
    with pytest.raises(ConfigError, match="backend"):
        parse_config_dict({"mode": "client", "backend": "bogus"})


def test_remote_backend_without_remote_section_raises():
    with pytest.raises(ConfigError, match="remote"):
        parse_config_dict({"mode": "client", "backend": "remote"})


def test_remote_section_missing_endpoint_raises():
    with pytest.raises(ConfigError, match="endpoint"):
        parse_config_dict(
            {
                "mode": "client",
                "backend": "remote",
                "remote": {"api_key_env": "X"},
            }
        )


def test_remote_section_missing_api_key_env_raises():
    with pytest.raises(ConfigError, match="api_key_env"):
        parse_config_dict(
            {
                "mode": "client",
                "backend": "remote",
                "remote": {"endpoint": "https://x"},
            }
        )


def test_top_level_must_be_a_mapping():
    with pytest.raises(ConfigError, match="mapping"):
        parse_config_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_partial_chunking_overrides_only_specified_fields():
    cfg = parse_config_dict({"mode": "client", "backend": "local", "chunking": {"workers": 4}})
    assert cfg.chunking.workers == 4
    assert cfg.chunking.minutes == 15  # default preserved
    assert cfg.chunking.snap_window_s == 10  # default preserved


# ---------------------------------------------------------------------------
# load_config — filesystem + precedence behavior
# ---------------------------------------------------------------------------


def test_load_config_missing_file_raises_actionable_error(tmp_path):
    missing = tmp_path / "tscribe.yaml"
    with pytest.raises(ConfigError, match="tscribe.example.yaml"):
        load_config(path=missing)


def test_load_config_reads_valid_yaml_file(tmp_path):
    config_file = tmp_path / "tscribe.yaml"
    config_file.write_text("mode: client\nbackend: local\n")
    cfg = load_config(path=config_file)
    assert cfg.mode == Mode.CLIENT
    assert cfg.backend == Backend.LOCAL


def test_load_config_malformed_yaml_raises_specific_error(tmp_path):
    config_file = tmp_path / "tscribe.yaml"
    config_file.write_text("mode: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path=config_file)


def test_load_config_env_var_points_at_file(tmp_path, monkeypatch):
    config_file = tmp_path / "custom-name.yaml"
    config_file.write_text("mode: server\nbackend: local\n")
    monkeypatch.setenv("TSCRIBE_CONFIG", str(config_file))
    cfg = load_config()
    assert cfg.mode == Mode.SERVER


def test_cli_override_takes_precedence_over_file(tmp_path):
    """Documented precedence: CLI flag > env var > config file > default."""
    config_file = tmp_path / "tscribe.yaml"
    config_file.write_text("mode: client\nbackend: local\n")
    cfg = load_config(path=config_file, overrides={"mode": "server"})
    assert cfg.mode == Mode.SERVER  # override wins
    assert cfg.backend == Backend.LOCAL  # untouched field preserved from file


def test_cli_override_merges_nested_sections_rather_than_replacing():
    cfg = parse_config_dict(
        {
            "mode": "client",
            "backend": "local",
            "local": {"model_size": "medium", "device": "cpu"},
        }
    )
    assert cfg.local.model_size == "medium"
    assert cfg.local.device == "cpu"
    assert cfg.local.compute_type == "int8"  # default, not clobbered


# ---------------------------------------------------------------------------
# config_to_dict — used by Unit 07's --json envelope
# ---------------------------------------------------------------------------


def test_config_to_dict_serializes_enums_to_their_values():
    cfg = parse_config_dict({"mode": "client", "backend": "local"})
    d = config_to_dict(cfg)
    assert d["mode"] == "client"
    assert d["backend"] == "local"
    assert isinstance(d["local"], dict)
    assert d["local"]["model_size"] == "small"


# ---------------------------------------------------------------------------
# ServerConfig (Unit 03) — added after Unit 01 shipped, per the "not yet
# modeled" placeholders in docs/config-migration.md
# ---------------------------------------------------------------------------


def test_server_config_defaults_when_section_omitted():
    cfg = parse_config_dict({"mode": "server", "backend": "local"})
    assert cfg.server.api_key_env == "TSCRIBE_API_KEY"
    assert cfg.server.max_audio_bytes == 2 * 1024**3
    assert cfg.server.download_timeout_s == 600
    assert cfg.server.drive_sa_json_env == "CARNYX_DRIVE_SA_JSON"


def test_server_config_section_overrides_defaults():
    cfg = parse_config_dict(
        {
            "mode": "server",
            "backend": "local",
            "server": {
                "api_key_env": "MY_CUSTOM_KEY",
                "max_audio_bytes": 1024,
                "download_timeout_s": 30,
                "drive_sa_json_env": "MY_SA_JSON",
            },
        }
    )
    assert cfg.server.api_key_env == "MY_CUSTOM_KEY"
    assert cfg.server.max_audio_bytes == 1024
    assert cfg.server.download_timeout_s == 30
    assert cfg.server.drive_sa_json_env == "MY_SA_JSON"


# ---------------------------------------------------------------------------
# Unit 01 — native TOML support (stdlib tomllib)
# ---------------------------------------------------------------------------

_TOML_FIXTURE = """\
mode = "client"
backend = "remote"

[remote]
endpoint = "https://tscribe.example.com"
api_key_env = "TSCRIBE_API_KEY"
kind = "tscribe_server"
transcription_model = "large-v3"

[local]
model_size = "medium"
device = "cpu"
compute_type = "int8"

[chunking]
minutes = 20
workers = 4
snap_window_s = 8

[summarization]
model = "lightweight"

[server]
api_key_env = "TSCRIBE_API_KEY"
max_audio_bytes = 1024
download_timeout_s = 30
drive_sa_json_env = "CARNYX_DRIVE_SA_JSON"

[capture]
mic_match = "Tula"
sample_rate = 16000
channels = 1
segment_seconds = 900
out_dir = "class_audio"
silence_warn_db = -50.0
silence_fail_db = -60.0
"""

_YAML_FIXTURE = """\
mode: client
backend: remote
remote:
  endpoint: https://tscribe.example.com
  api_key_env: TSCRIBE_API_KEY
  kind: tscribe_server
  transcription_model: large-v3
local:
  model_size: medium
  device: cpu
  compute_type: int8
chunking:
  minutes: 20
  workers: 4
  snap_window_s: 8
summarization:
  model: lightweight
server:
  api_key_env: TSCRIBE_API_KEY
  max_audio_bytes: 1024
  download_timeout_s: 30
  drive_sa_json_env: CARNYX_DRIVE_SA_JSON
capture:
  mic_match: Tula
  sample_rate: 16000
  channels: 1
  segment_seconds: 900
  out_dir: class_audio
  silence_warn_db: -50.0
  silence_fail_db: -60.0
"""


def test_load_config_toml_and_yaml_construct_identical_config(tmp_path):
    """Unit 01's headline criterion: an equivalent TOML and YAML fixture
    produce a byte-for-byte identical RuntimeConfig."""
    toml_path = tmp_path / "tscribe.toml"
    yaml_path = tmp_path / "tscribe.yaml"
    toml_path.write_text(_TOML_FIXTURE)
    yaml_path.write_text(_YAML_FIXTURE)

    from_toml = load_config(path=toml_path, allow_project_layering=False)
    from_yaml = load_config(path=yaml_path, allow_project_layering=False)

    assert from_toml == from_yaml
    assert from_toml.remote is not None
    assert from_toml.remote.kind == RemoteKind.TSCRIBE_SERVER
    assert from_toml.capture.out_dir == "class_audio"


def test_load_config_text_parses_toml_and_yaml_by_hint():
    toml = load_config_text(_TOML_FIXTURE, filename_hint="tscribe.toml")
    yaml_data = load_config_text(_YAML_FIXTURE, filename_hint="tscribe.yaml")
    assert parse_config_dict(toml) == parse_config_dict(yaml_data)


def test_legacy_yaml_and_yml_parse_identically(tmp_path):
    yml_path = tmp_path / "tscribe.yml"
    yaml_path = tmp_path / "tscribe.yaml"
    yml_path.write_text(_YAML_FIXTURE)
    yaml_path.write_text(_YAML_FIXTURE)
    assert load_config(path=yml_path, allow_project_layering=False) == load_config(
        path=yaml_path, allow_project_layering=False
    )


def test_load_config_toml_file_missing_required_field_is_specific(tmp_path):
    config_file = tmp_path / "tscribe.toml"
    config_file.write_text('mode = "client"\n')
    with pytest.raises(ConfigError, match="backend"):
        load_config(path=config_file, allow_project_layering=False)


def test_toml_invalid_syntax_raises_config_error():
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config_text("mode = [unclosed\n", filename_hint="tscribe.toml")


def test_toml_unknown_top_level_field_raises_config_error():
    with pytest.raises(ConfigError, match="unknown top-level field"):
        load_config_text('mode = "client"\nbackend = "local"\nbogus = 1\n', filename_hint="x.toml")


def test_toml_unknown_section_field_raises_config_error():
    with pytest.raises(ConfigError, match="unknown field"):
        load_config_text(
            'mode = "client"\nbackend = "local"\n[local]\nnot_a_field = 1\n',
            filename_hint="x.toml",
        )


def test_toml_invalid_enum_value_raises_config_error():
    with pytest.raises(ConfigError, match="backend"):
        parse_config_dict(
            load_config_text('mode = "client"\nbackend = "bogus"\n', filename_hint="x.toml")
        )


def test_yaml_unknown_fields_remain_tolerated_for_backwards_compat():
    """Strict unknown-field rejection is scoped to `.toml` (the new format).
    A legacy `.yaml` with an unrecognized key keeps loading, preserving the
    documented hard backwards-compatibility guarantee."""
    cfg = parse_config_dict(
        load_config_text("mode: client\nbackend: local\nlegacy_key: 1\n", filename_hint="x.yaml")
    )
    assert cfg.mode == Mode.CLIENT


def test_load_config_text_rejects_unknown_extension():
    with pytest.raises(ConfigError, match="Unsupported config file extension"):
        load_config_text("{}", filename_hint="tscribe.json")


def test_resolve_config_path_prefers_toml_over_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("TSCRIBE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tscribe.yaml").write_text("mode: client\nbackend: local\n")
    assert _resolve_config_path(None).name == "tscribe.yaml"  # only YAML present
    (tmp_path / "tscribe.toml").write_text('mode = "client"\nbackend = "local"\n')
    assert _resolve_config_path(None).name == "tscribe.toml"  # TOML wins


# ---------------------------------------------------------------------------
# Unit 02 — hierarchical base + project-local layering
# ---------------------------------------------------------------------------


def _write_base(home: Path, text: str) -> Path:
    base_dir = home / ".config" / "tscribe"
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / "config.toml"
    path.write_text(text)
    return path


def test_hierarchical_base_only_loading(tmp_path, monkeypatch):
    """Criterion 1: with no project file in cwd, the user-global base config
    is discovered without setting $TSCRIBE_CONFIG."""
    home = tmp_path / "home"
    _write_base(home, 'mode = "client"\nbackend = "local"\n[local]\nmodel_size = "small"\n')
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TSCRIBE_CONFIG", raising=False)

    cfg = load_config(cwd=project)
    assert cfg.local.model_size == "small"
    assert default_base_config_path() == home / ".config" / "tscribe" / "config.toml"


def test_layering_project_overlay_merges_partial_fields(tmp_path, monkeypatch):
    """Criteria 2 & 3: a cwd `.tscribe.toml` that names only a couple of
    fields is deep-merged on top of the base, leaving everything else (mode,
    backend, other sections) intact."""
    home = tmp_path / "home"
    _write_base(
        home,
        'mode = "client"\nbackend = "local"\n[local]\nmodel_size = "small"\n'
        "[chunking]\nworkers = 2\nminutes = 15\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tscribe.toml").write_text("[chunking]\nworkers = 8\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TSCRIBE_CONFIG", raising=False)

    cfg = load_config(cwd=project)
    assert cfg.mode == Mode.CLIENT  # from base, not repeated in override
    assert cfg.local.model_size == "small"  # untouched base value
    assert cfg.chunking.workers == 8  # overridden
    assert cfg.chunking.minutes == 15  # sibling preserved
    assert project_override_path(project) == project / ".tscribe.toml"


def test_hierarchical_precedence_explicit_over_env_over_base_project(tmp_path, monkeypatch):
    """Criterion 4's precedence chain: explicit path > $TSCRIBE_CONFIG >
    base + project overlay."""
    home = tmp_path / "home"
    _write_base(home, 'mode = "client"\nbackend = "local"\n[local]\nmodel_size = "small"\n')
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tscribe.toml").write_text('[local]\nmodel_size = "medium"\n')
    env_file = tmp_path / "env.toml"
    env_file.write_text('mode = "client"\nbackend = "local"\n[local]\nmodel_size = "large"\n')
    explicit_file = tmp_path / "explicit.toml"
    explicit_file.write_text('mode = "client"\nbackend = "local"\n[local]\nmodel_size = "tiny"\n')
    monkeypatch.setenv("HOME", str(home))

    monkeypatch.setenv("TSCRIBE_CONFIG", str(env_file))
    assert load_config(cwd=project).local.model_size == "large"
    # The CLI passes allow_project_layering=False for an explicit --config
    # (unless --with-project-config), so that is the shape asserted here.
    assert (
        load_config(path=explicit_file, cwd=project, allow_project_layering=False).local.model_size
        == "tiny"
    )

    monkeypatch.delenv("TSCRIBE_CONFIG", raising=False)
    assert load_config(cwd=project).local.model_size == "medium"  # project over base


def test_layering_explicit_path_bypasses_project_overlay_unless_enabled(tmp_path, monkeypatch):
    """Criterion 4: an explicit path ignores the cwd project override unless
    layering is explicitly enabled (`allow_project_layering=True`, wired to
    the CLI's --with-project-config)."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tscribe.toml").write_text('[local]\nmodel_size = "large"\n')
    explicit_file = tmp_path / "explicit.toml"
    explicit_file.write_text('mode = "client"\nbackend = "local"\n[local]\nmodel_size = "tiny"\n')
    monkeypatch.delenv("TSCRIBE_CONFIG", raising=False)

    without = load_config(path=explicit_file, cwd=project, allow_project_layering=False)
    assert without.local.model_size == "tiny"  # project bypassed
    with_layer = load_config(path=explicit_file, cwd=project, allow_project_layering=True)
    assert with_layer.local.model_size == "large"  # project layered on top


def test_layering_env_var_bypasses_project_overlay(tmp_path, monkeypatch):
    """$TSCRIBE_CONFIG stays authoritative (no project overlay), preserving
    the legacy single-file contract."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tscribe.toml").write_text('[local]\nmodel_size = "large"\n')
    env_file = tmp_path / "env.toml"
    env_file.write_text('mode = "client"\nbackend = "local"\n[local]\nmodel_size = "large"\n')
    monkeypatch.setenv("TSCRIBE_CONFIG", str(env_file))
    cfg = load_config(cwd=project)
    assert cfg.local.model_size == "large"
    # env file value wins over project file value (proving project was not merged)
    env_file.write_text('mode = "client"\nbackend = "local"\n[local]\nmodel_size = "tiny"\n')
    assert load_config(cwd=project).local.model_size == "tiny"


def test_hierarchical_no_config_anywhere_raises_actionable_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TSCRIBE_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="tscribe.yaml"):
        load_config(cwd=project)
